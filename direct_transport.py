"""Direct SMTP delivery: bounded DNS/network work and explicit delivery outcomes.

No database writes here. A caller must persist 'sending' BEFORE calling deliver
and persist the result afterwards. A lost DATA acknowledgement is uncertain,
never evidence that trying a different MX is safe.
"""
import base64
from email import policy
from email.parser import BytesParser
from email.utils import formatdate, getaddresses, make_msgid
import ipaddress
from pathlib import Path
import random
import re
import smtplib
import socket
import ssl
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import dkim
import dns.exception
import dns.resolver
from email_validator import validate_email

MAX_BYTES = 25_000_000
MAX_RECIPIENTS = 100


def domain_name(value):
    value = value.rstrip('.').encode('idna').decode('ascii').lower()
    if (len(value) > 253 or not re.fullmatch(
            r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
            r'(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+', value)):
        raise ValueError('A fully qualified domain name is required')
    return value


def addresses(values):
    values = [value for value in values if value.strip()]
    result = []
    if any('\r' in value or '\n' in value for value in values):
        raise ValueError('Invalid envelope address')
    for _, address in getaddresses(values):
        if not address and not _:
            continue
        normalized = validate_email(address, check_deliverability=False,
            allow_smtputf8=False, test_environment=True).ascii_email
        if not normalized:
            raise ValueError('SMTPUTF8 envelope addresses are not supported')
        result.append(normalized)
    return list(dict.fromkeys(result))


def prepare(raw, sender, recipients, domain):
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    raw = bytes(raw)
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('Message is empty or exceeds 25 MB')
    senders = addresses([sender])
    if len(senders) != 1 or senders[0].rsplit('@', 1)[1] != domain:
        raise ValueError('Envelope sender is not in the configured domain')
    recipients = addresses(recipients)
    if not 1 <= len(recipients) <= MAX_RECIPIENTS:
        raise ValueError('Between 1 and 100 recipients are required')
    message = BytesParser(policy=policy.SMTP.clone(cte_type='7bit')).parsebytes(raw)
    if any(part.defects for part in message.walk()):
        raise ValueError('Malformed MIME message')
    if len(message.get_all('From', [])) != 1:
        raise ValueError('Exactly one From header is required')
    froms = addresses([str(message['From'])])
    if len(froms) != 1 or froms[0].rsplit('@', 1)[1] != domain:
        raise ValueError('From is not in the configured domain')
    for name in ('Sender', 'Date', 'Message-ID', 'Subject', 'Reply-To'):
        if len(message.get_all(name, [])) > 1:
            raise ValueError('Duplicate ' + name + ' header')
    if message.get('Sender'):
        sender_header = addresses([str(message['Sender'])])
        if len(sender_header) != 1 or sender_header[0].rsplit('@', 1)[1] != domain:
            raise ValueError('Sender header is not in the configured domain')
    if any(name.lower().startswith('resent-') for name in message.keys()):
        raise ValueError('Resent headers are not supported')
    for name in ('Bcc', 'Return-Path', 'DKIM-Signature', 'Authentication-Results'):
        del message[name]
    if not message.get('Date'):
        message['Date'] = formatdate(localtime=False)
    if not message.get('Message-ID'):
        message['Message-ID'] = make_msgid(domain=domain)
    wire = message.as_bytes()
    # Portable 7-bit MIME avoids depending on 8BITMIME at remote MXs.
    wire.decode('ascii')
    if len(wire) > MAX_BYTES or any(len(line) > 998 for line in wire.split(b'\r\n')):
        raise ValueError('Message exceeds SMTP size or line length limits')
    return wire, senders[0], recipients, str(message['Message-ID'])


def load_key(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('DKIM key must be an absolute non-symlink path')
    if path.stat().st_mode & 0o027:
        raise ValueError('DKIM key must not be world-accessible or group-writable')
    private = path.read_bytes()
    key = serialization.load_pem_private_key(private, password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ValueError('DKIM requires an RSA private key of at least 2048 bits')
    public = key.public_key().public_bytes(serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    return private, 'v=DKIM1; k=rsa; p=' + base64.b64encode(public).decode()


def sign(wire, domain, selector, key_path, resolver):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,62}', selector):
        raise ValueError('Invalid DKIM selector')
    private, public = load_key(key_path)
    records = resolver.resolve(selector + '._domainkey.' + domain,
        'TXT', lifetime=5)
    expected = public.split('p=', 1)[1]
    published = [b''.join(record.strings) for record in records
        if b'v=DKIM1' in b''.join(record.strings)]
    if len(published) != 1 or not any(dict(part.strip().split('=', 1) for part in
            value.decode('ascii').split(';') if '=' in part
            ).get('p', '').replace(' ', '') == expected for value in published):
        raise ValueError('Published DKIM key does not match the private key')
    # Oversign identity/content headers, including absent headers, to prevent
    # adding a second From/Subject/etc after signing.
    headers = [b'from', b'to', b'cc', b'sender', b'reply-to', b'subject',
        b'date', b'message-id', b'mime-version', b'content-type',
        b'content-transfer-encoding']
    signed = dkim.sign(wire, selector.encode(), domain.encode(), private,
        canonicalize=(b'relaxed', b'relaxed'), signature_algorithm=b'rsa-sha256',
        include_headers=headers + headers) + wire
    if not dkim.verify(signed, dnsfunc=lambda *a, **kw: published[0]):
        raise ValueError('Generated DKIM signature failed verification')
    return signed


class PinnedSMTP(smtplib.SMTP):
    """Connect to the already vetted address; retain MX hostname for TLS SNI."""
    def __init__(self, hostname, address, helo, timeout):
        self.address = address
        self.reply_deadline = time.monotonic() + timeout
        super().__init__(hostname, 25, local_hostname=helo, timeout=timeout)

    def _get_socket(self, host, port, timeout):
        return socket.create_connection((self.address, port), timeout)

    def getreply(self):
        # Bound multiline replies as well as individual socket reads. A peer
        # continuously sending continuation lines must not exceed the time budget.
        if self.file is None:
            self.file = self.sock.makefile('rb')
        replies = []
        for _ in range(100):
            remaining = self.reply_deadline - time.monotonic()
            if remaining <= 0:
                raise smtplib.SMTPServerDisconnected('Reply deadline exceeded')
            self.sock.settimeout(min(self.timeout, remaining))
            line = self.file.readline(8193)
            if not line or len(line) > 8192 or not line[:3].isdigit():
                raise smtplib.SMTPServerDisconnected('Invalid or truncated SMTP reply')
            replies.append(line[4:].strip())
            if line[3:4] != b'-':
                return int(line[:3]), b'\n'.join(replies)
        raise smtplib.SMTPServerDisconnected('Too many SMTP reply lines')


def mx_addresses(domain, resolver):
    try:
        records = resolver.resolve(domain, 'MX', lifetime=5)
        if any(str(record.exchange) == '.' for record in records):
            raise ValueError('Domain publishes Null MX and accepts no mail')
        hosts = [(record.preference, str(record.exchange).rstrip('.'))
            for record in records]
        random.SystemRandom().shuffle(hosts)
        hosts.sort(key=lambda item: item[0])
    except dns.resolver.NoAnswer:
        hosts = [(0, domain)]
    # Bound malicious DNS responses while keeping the highest-priority MXs.
    transient = False
    usable = False
    for _, host in hosts[:10]:
        host = domain_name(host)
        for kind in ('AAAA', 'A'):
            try:
                records = resolver.resolve(host, kind, lifetime=5)
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
                continue
            except dns.exception.DNSException:
                transient = True
                continue
            for record in list(records)[:4]:
                address = str(record)
                ip = ipaddress.ip_address(address)
                if not ip.is_global or ip.is_multicast:
                    # Remote-controlled DNS must not turn the worker into an
                    # internal network scanner (also no second DNS lookup).
                    continue
                usable = True
                yield host, address
    if not usable:
        if transient:
            raise dns.exception.Timeout('MX address lookup temporarily failed')
        raise ValueError('No public delivery address for recipient domain')


def deliver(wire, sender, recipient, helo, timeout=60, require_tls=True,
        resolver=None, smtp_factory=None, budget=300, envid=None):
    resolver = resolver or dns.resolver.Resolver()
    smtp_factory = smtp_factory or PinnedSMTP
    deadline = time.monotonic() + budget
    last_error = 'No reachable MX'
    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('Delivery attempt time budget exceeded')
        return min(timeout, left)
    try:
        candidates = mx_addresses(recipient.rsplit('@', 1)[1], resolver)
        for hostname, address in candidates:
            client = None
            phase = 'connect'
            try:
                client = smtp_factory(hostname, address, helo, remaining())
                client.sock.settimeout(remaining())
                code, reply = client.ehlo()
                if code != 250:
                    raise smtplib.SMTPHeloError(code, reply)
                if client.has_extn('starttls'):
                    client.sock.settimeout(remaining())
                    client.starttls(context=ssl.create_default_context())
                    client.sock.settimeout(remaining())
                    code, reply = client.ehlo()
                    if code != 250:
                        raise smtplib.SMTPHeloError(code, reply)
                elif require_tls:
                    last_error = 'MX does not offer required STARTTLS'
                    continue
                options = []
                if client.has_extn('size'):
                    limit = client.esmtp_features.get('size', '')
                    if limit.isdigit() and int(limit) and len(wire) > int(limit):
                        return 'failed', '552 Message exceeds MX size limit'
                    options.append('SIZE=' + str(len(wire)))
                rcpt_options = []
                if envid and client.has_extn('dsn'):
                    options.extend(['RET=HDRS', 'ENVID=' + envid])
                    rcpt_options.append('NOTIFY=FAILURE,DELAY')
                phase = 'envelope'
                for command, args in [(client.mail, (sender, options)),
                        (client.rcpt, (recipient, rcpt_options))]:
                    client.sock.settimeout(remaining())
                    code, reply = command(*args)
                    if code not in (250, 251, 252):
                        raise smtplib.SMTPResponseException(code, reply)
                # Once DATA is attempted, a disconnect may mean acceptance.
                # Conservatively hold it for operator review, never fail over.
                phase = 'data'
                client.sock.settimeout(remaining())
                code, reply = client.data(wire)
                if code == 250:
                    return 'sent', '250 Accepted by ' + hostname
                raise smtplib.SMTPDataError(code, reply)
            except smtplib.SMTPResponseException as error:
                detail = error.smtp_error.decode('utf-8', 'replace') if isinstance(
                    error.smtp_error, bytes) else str(error.smtp_error)
                last_error = f'{error.smtp_code} {detail}'[:1000]
                if 500 <= error.smtp_code < 600 and phase != 'connect':
                    return 'failed', last_error
                if phase == 'data' and not 400 <= error.smtp_code < 600:
                    return 'uncertain', last_error
                # A definite negative DATA response makes retry safe.
            except (OSError, smtplib.SMTPException) as error:
                last_error = f'{type(error).__name__}: {error}'[:1000]
                if phase == 'data':
                    return 'uncertain', last_error
            finally:
                if client is not None:
                    client.close()  # QUIT failure cannot undo DATA acceptance.
            if time.monotonic() >= deadline:
                break
    except dns.resolver.NXDOMAIN:
        return 'failed', 'Recipient domain does not exist'
    except ValueError as error:
        return 'failed', str(error)
    except dns.exception.DNSException as error:
        last_error = 'Temporary DNS failure: ' + str(error)
    return 'pending', last_error[:1000]
