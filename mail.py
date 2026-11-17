"""Mail dispatch and independent, durable direct deliveries per recipient."""
from datetime import datetime, timedelta
from email.parser import BytesParser
from email.utils import getaddresses
import random
import re
import secrets

from trytond import backend
from trytond.config import config
from trytond.exceptions import UserError
from trytond.model import Index, ModelSQL, ModelView, Unique, fields
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Bool, Eval
from trytond.transaction import Transaction, TransactionError

from . import direct_transport as transport

_INTERNAL = object()
PROTECTED = {'delivery_envelope', 'direct_deliveries', 'delivery_token',
    'delivery_report', 'delivery_state', 'delivery_due', 'delivery_expires'}
CONTENT = {'mail_file', 'mail_file_id', 'from_', 'sender', 'to', 'cc', 'bcc',
    'message_id', 'delivery_server', 'flag_send'}


class Server(metaclass=PoolMeta):
    __name__ = 'smtp.server'

    direct_delivery = fields.Boolean('Deliver Directly to MX',
        states={'readonly': Eval('state') != 'draft'})
    direct_domain = fields.Char('Sending Domain',
        states={'required': Eval('direct_delivery', False),
            'readonly': Eval('state') != 'draft'})
    direct_hostname = fields.Char('Public SMTP Hostname',
        states={'required': Eval('direct_delivery', False),
            'readonly': Eval('state') != 'draft'})
    direct_selector = fields.Char('DKIM Selector',
        states={'required': Eval('direct_delivery', False),
            'readonly': Eval('state') != 'draft'})
    direct_key = fields.Char('DKIM Key Configuration Name',
        states={'required': Eval('direct_delivery', False),
            'readonly': Eval('state') != 'draft'},
        help='Entry in the smtp_dkim configuration section. Private keys '
        'are read from server files, never stored in the database.')
    direct_dns = fields.Function(fields.Text('DKIM DNS Record'), 'get_direct_dns')
    direct_mailbox = fields.Many2One('electronic.mail.mailbox', 'Outbox Mailbox',
        states={'required': Eval('direct_delivery', False)})
    direct_require_tls = fields.Boolean('Require Verified TLS',
        help='If disabled, plaintext is allowed only when the MX does not '
        'advertise STARTTLS. TLS negotiation failures never fall back to plaintext.')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.smtp_server.required = False
        cls.smtp_server.states['required'] = ~Eval('direct_delivery', False)
        cls._buttons['get_smtp_test']['invisible'] = Eval('direct_delivery', False)

    @staticmethod
    def default_direct_require_tls():
        return True

    @classmethod
    def validate_fields(cls, records, field_names):
        super().validate_fields(records, field_names)
        for server in records:
            if server.direct_delivery:
                try:
                    for name in ('direct_domain', 'direct_hostname'):
                        value = getattr(server, name)
                        if transport.domain_name(value or '') != value:
                            raise ValueError('Use lowercase ASCII domain names')
                    if getattr(server, 'nantic_smtp', False):
                        raise ValueError('Direct MX and NaN-tic SMTP are exclusive')
                    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,62}',
                            server.direct_selector or ''):
                        raise ValueError('Invalid DKIM selector')
                    if not 1 <= server.smtp_timeout <= 300:
                        raise ValueError('SMTP timeout must be between 1 and 300 seconds')
                except ValueError as error:
                    raise UserError(str(error)) from error

    def get_direct_dns(self, name):
        if not self.direct_delivery or not self.direct_key:
            return ''
        path = config.get('smtp_dkim', self.direct_key)
        if not path:
            return 'Configure the DKIM key file in [smtp_dkim].'
        try:
            _, public = transport.load_key(path)
        except (OSError, ValueError):
            return 'DKIM key unavailable or invalid; check the server configuration.'
        return f'{self.direct_selector}._domainkey.{self.direct_domain} TXT {public}'

    def get_smtp_server(self):
        if self.direct_delivery:
            raise UserError('Direct MX delivery uses the electronic.mail outbox.')
        return super().get_smtp_server()

    def send_mail(self, from_, cc, email, mail=None):
        if mail is None and not self.direct_delivery:
            return super().send_mail(from_, cc, email)
        Mail = Pool().get('electronic.mail')
        if mail is None:
            # Queue creation participates in the caller's transaction.
            raw = email.encode() if isinstance(email, str) else bytes(email)
            mail = Mail.create_from_mail(BytesParser().parsebytes(raw),
                self.direct_mailbox.id)
        Mail.send_mail([mail], server=self, sender=from_, recipients=cc)
        return True  # accepted into the outbox, not yet delivered


class Mail(metaclass=PoolMeta):
    __name__ = 'electronic.mail'

    delivery_token = fields.Char('Delivery Envelope ID', readonly=True)
    delivery_report = fields.Selection([('', 'Not a Report'),
        ('pending', 'Pending'), ('done', 'Processed')], 'Delivery Report', readonly=True)
    delivery_server = fields.Many2One('smtp.server', 'Delivery Server',
        ondelete='RESTRICT', states={'readonly': Eval('delivery_state', '') != ''})
    direct_deliveries = fields.One2Many('electronic.mail.direct', 'mail',
        'Direct Deliveries', readonly=True)
    delivery_state = fields.Function(fields.Selection([
        ('', 'Not Queued'), ('queued', 'Queued'), ('sending', 'Sending'),
        ('sent', 'Accepted by MX'), ('partial', 'Partially Delivered'),
        ('failed', 'Failed'), ('uncertain', 'Needs Review'),
        ('cancelled', 'Cancelled')], 'Delivery State'), 'get_delivery')
    delivery_envelope = fields.Char('Envelope Sender', readonly=True)
    delivery_due = fields.Function(fields.DateTime('Next Attempt'), 'get_delivery')
    delivery_expires = fields.Function(fields.DateTime('Delivery Deadline'), 'get_delivery')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_indexes.add(Index(table, (table.delivery_token, Index.Equality())))
        cls._buttons.update({
            'send_mail': {'invisible': ~Bool(Eval('body')) | Eval('flag_send')
                | (Eval('delivery_state', '') != '')},
            'cancel_direct': {'invisible': ~Eval('delivery_state').in_(
                ['queued', 'sending', 'uncertain', 'partial', 'failed'])},
            'retry_direct': {'invisible': ~Eval('delivery_state').in_(
                ['sending', 'uncertain', 'partial', 'failed', 'cancelled'])},
            })

    def get_delivery(self, name):
        deliveries = self.direct_deliveries
        if name == 'delivery_due':
            return min((min(d.next_attempt, d.expires) for d in deliveries
                if d.state == 'pending'), default=None)
        if name == 'delivery_expires':
            return max((d.expires for d in deliveries), default=None)
        states = {d.state for d in deliveries}
        if not states:
            return ''
        if 'sending' in states:
            return 'sending'
        if 'pending' in states:
            return 'queued'
        if states == {'sent'}:
            return 'sent'
        if 'uncertain' in states:
            return 'uncertain'
        if 'sent' in states:
            return 'partial'
        if states == {'cancelled'}:
            return 'cancelled'
        return 'failed'

    @classmethod
    def check_modification(cls, mode, records, values=None, external=False):
        super().check_modification(mode, records, values=values, external=external)
        if mode == 'delete':
            if any(d.state in {'pending', 'sending', 'uncertain'}
                    for mail in records for d in mail.direct_deliveries):
                raise UserError('Cancel or resolve pending deliveries before deleting mail.')
        elif Transaction().context.get('_direct_internal') is not _INTERNAL:
            if mode == 'create':
                if any(getattr(mail, name) for mail in records for name in PROTECTED):
                    raise UserError('Use the outbox actions to change delivery state.')
            elif mode == 'write':
                if PROTECTED.intersection(values):
                    raise UserError('Use the outbox actions to change delivery state.')
                if CONTENT.intersection(values) and any(m.direct_deliveries for m in records):
                    raise UserError('Queued message content is immutable; create a new mail.')

    @classmethod
    def copy(cls, mails, default=None):
        default = dict(default or {})
        default.update(delivery_envelope=None, delivery_token=None,
            delivery_report=None, direct_deliveries=None, flag_send=False)
        with Transaction().set_context(_direct_internal=_INTERNAL):
            return super().copy(mails, default=default)

    @classmethod
    def _get_sender_and_recipients(cls, mail):
        sender = cls.validate_emails(
            (mail.from_ or '').strip(), raise_exception=False)
        recipients = [address.strip() for _, address in getaddresses(
            [value for value in (mail.to, mail.cc, mail.bcc)
                if value and value.strip()]) if address.strip()]
        return sender, recipients

    @classmethod
    def _get_delivery_settings(cls, mail):
        if mail.delivery_server:
            return mail.delivery_server, None
        servers = Pool().get('smtp.server').search([
            ('state', '=', 'done'), ('default', '=', True)], limit=1)
        return (servers[0] if servers else None,
            Pool().get('electronic.mail.configuration')(1).draft)

    @classmethod
    @ModelView.button
    def send_mail(cls, mails, server=None, sender=None, recipients=None):
        """Queue mail through either transport; workers manage flag_send.

        Callers supply mail and optional envelope/server overrides. They must
        neither inspect the transport nor update flag_send themselves.
        """
        if not config.getboolean('database', 'production', default=False):
            return
        delay = Pool().get('electronic.mail.configuration')(1).send_email_after
        for mail in mails:
            if mail.flag_send or mail.direct_deliveries:
                continue
            with Transaction().set_context(
                    queue_name=config.get('electronic_mail', 'queue_name', default='default'),
                    queue_scheduled_at=delay):
                cls.__queue__._send_mail([mail],
                    server_id=server.id if server else None,
                    sender=sender, recipients=recipients)

    @classmethod
    def _send_mail(cls, mails, server_id=None, sender=None, recipients=None):
        """Worker-only dispatch: relay SMTP or create one task per recipient."""
        Direct = Pool().get('electronic.mail.direct')
        for record in mails:
            cls.lock([record])
            mail = cls(record.id)
            if mail.flag_send or mail.direct_deliveries:
                continue
            server, draft = cls._get_delivery_settings(mail) if not server_id else (
                Pool().get('smtp.server')(server_id), None)
            if not server or server.state != 'done':
                raise UserError('Configure a confirmed SMTP server before sending mail.')
            with Transaction().set_context({'electronic.mail.mail_file': None}):
                raw = cls(mail.id).mail_file
            if not raw:
                raise UserError('The email has no MIME content.')
            default_sender, default_recipients = cls._get_sender_and_recipients(mail)
            envelope = sender if sender is not None else default_sender
            rcpts = recipients if recipients is not None else default_recipients
            if not envelope or not rcpts or not cls.validate_emails(rcpts, raise_exception=False):
                if draft:
                    cls.write([mail], {'mailbox': draft.id})
                continue
            if not config.getboolean('database', 'production', default=False):
                raise UserError('Mail delivery is disabled outside production.')
            if not server.direct_delivery:
                server.send_mail(envelope, rcpts, raw)
                cls.write([mail], {'flag_send': True})
                continue
            if not config.getboolean('queue', 'worker', default=False):
                raise UserError('Direct delivery requires [queue] worker = True.')
            if mail.flag_received or mail.mailbox != server.direct_mailbox:
                raise UserError('Use the configured outbox for unsent outgoing mail.')
            try:
                wire, envelope, rcpts, message_id = transport.prepare(
                    raw, envelope, rcpts, server.direct_domain)
            except (ValueError, TypeError) as error:
                raise UserError(str(error)) from error
            now = datetime.utcnow()
            with Transaction().set_context(_direct_internal=_INTERNAL):
                cls.write([mail], {'delivery_server': server.id,
                    'delivery_envelope': envelope, 'mail_file': wire,
                    'size': len(wire), 'message_id': message_id,
                    'delivery_token': secrets.token_hex(24),
                    'flag_send': False, 'flag_draft': False})
                deliveries = Direct.create([{'mail': mail.id, 'recipient': address,
                    'state': 'pending', 'attempts': 0, 'next_attempt': now,
                    'expires': now + timedelta(days=5)} for address in rcpts])
            for delivery in deliveries:
                delivery._schedule()

    @classmethod
    def _update_delivery(cls, mails):
        # A separate queue task updates the legacy flag. Its rollback cannot
        # discard a recipient's already committed SMTP result.
        for record in mails:
            cls.lock([record])
            mail = cls(record.id)
            with Transaction().set_context(_direct_internal=_INTERNAL):
                cls.write([mail], {'flag_send': bool(mail.direct_deliveries)
                    and all(d.state == 'sent' for d in mail.direct_deliveries)})

    @classmethod
    @ModelView.button
    def cancel_direct(cls, mails):
        cls._reschedule(mails, retry=False)

    @classmethod
    @ModelView.button
    def retry_direct(cls, mails):
        cls._reschedule(mails, retry=True)

    @classmethod
    def _reschedule(cls, mails, retry):
        Direct = Pool().get('electronic.mail.direct')
        now = datetime.utcnow()
        # Manual actions intentionally do not lock running queue tasks.
        for mail in mails:
            for delivery in mail.direct_deliveries:
                if delivery.state == 'sent':
                    continue
                with Transaction().set_context(_direct_internal=_INTERNAL):
                    Direct.write([delivery], {'state': 'pending' if retry else 'cancelled',
                        'next_attempt': now if retry else None, 'attempts': 0,
                        'expires': now + timedelta(days=5),
                        'history': delivery._history('manual retry' if retry else 'cancel')})
                if retry:
                    delivery._schedule()
            with Transaction().set_context(_direct_internal=None):
                cls.__queue__._update_delivery([mail])

    @classmethod
    def create_from_mail(cls, message, mailbox, record=None):
        mail = super().create_from_mail(message, mailbox, record=record)
        if (mail and message.get_content_type() == 'multipart/report'
                and message.get_param('report-type') == 'delivery-status'):
            with Transaction().set_context(_direct_internal=_INTERNAL):
                cls.write([mail], {'delivery_report': 'pending'})
            cls.__queue__._process_reports([mail])
        return mail

    @classmethod
    def _process_reports(cls, reports):
        Direct = Pool().get('electronic.mail.direct')
        for report_mail in reports:
            cls.lock([report_mail])
            if report_mail.delivery_report != 'pending':
                continue
            with Transaction().set_context({'electronic.mail.mail_file': None}):
                raw = cls(report_mail.id).mail_file
            retry = False
            if raw and len(raw) <= transport.MAX_BYTES:
                report = BytesParser().parsebytes(bytes(raw))
                for part in report.walk():
                    if part.get_content_type() != 'message/delivery-status':
                        continue
                    blocks = part.get_payload()
                    if not isinstance(blocks, list) or not blocks:
                        continue
                    token = str(blocks[0].get('Original-Envelope-ID', '')).strip()
                    if len(token) != 48:
                        continue
                    for block in blocks[1:]:
                        address = str(block.get('Final-Recipient', '')).partition(';')[2].strip()
                        action = str(block.get('Action', '')).strip().lower()
                        if action not in {'failed', 'delayed'}:
                            continue
                        matches = Direct.search([('mail.delivery_token', '=', token),
                            ('recipient', '=', address)], limit=1)
                        if not matches:
                            continue
                        Direct.lock(matches)
                        delivery = Direct(matches[0].id)
                        if delivery.state == 'sending':
                            retry = True
                            continue
                        if delivery.state not in {'sent', 'uncertain'}:
                            continue
                        detail = str(block.get('Diagnostic-Code', block.get('Status', '')))[:1000]
                        with Transaction().set_context(_direct_internal=_INTERNAL):
                            Direct.write([delivery], {
                                'state': 'failed' if action == 'failed' else delivery.state,
                                'error': 'DSN ' + action + ': ' + detail,
                                'history': delivery._history(
                                    f'DSN {action} (report {report_mail.id}): {detail}')})
                        cls.__queue__._update_delivery([delivery.mail])
            if retry:
                with Transaction().set_context(queue_scheduled_at=timedelta(minutes=10)):
                    cls.__queue__._process_reports([report_mail])
            else:
                with Transaction().set_context(_direct_internal=_INTERNAL):
                    cls.write([report_mail], {'delivery_report': 'done'})


class Direct(ModelSQL, ModelView):
    "Direct Mail Delivery"
    __name__ = 'electronic.mail.direct'

    mail = fields.Many2One('electronic.mail', 'Mail', required=True,
        ondelete='CASCADE', readonly=True)
    recipient = fields.Char('Recipient', required=True, readonly=True)
    state = fields.Selection([('pending', 'Pending'), ('sending', 'Sending'),
        ('sent', 'Accepted by MX'), ('failed', 'Failed'),
        ('uncertain', 'Needs Review'), ('cancelled', 'Cancelled')],
        'State', required=True, readonly=True)
    attempts = fields.Integer('Attempts', readonly=True)
    next_attempt = fields.DateTime('Next Attempt', readonly=True)
    expires = fields.DateTime('Delivery Deadline', required=True, readonly=True)
    started = fields.DateTime('Attempt Started', readonly=True)
    error = fields.Text('Last Error', readonly=True)
    history = fields.Text('History', readonly=True)
    task_token = fields.Char('Attempt Token', readonly=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.__access__.add('mail')
        table = cls.__table__()
        cls._sql_constraints.append(('mail_recipient_unique',
            Unique(table, table.mail, table.recipient), 'smtp.msg_direct_recipient_unique'))
        cls._sql_indexes.add(Index(table, (table.mail, Index.Equality())))

    @classmethod
    def check_modification(cls, mode, records, values=None, external=False):
        super().check_modification(mode, records, values=values, external=external)
        if Transaction().context.get('_direct_internal') is not _INTERNAL:
            raise UserError('Use the mail actions to manage direct deliveries.')

    def _history(self, detail):
        line = f'{datetime.utcnow().isoformat()} {detail}'
        return '\n'.join(((self.history or '').splitlines() + [line])[-200:])

    def _schedule(self):
        token = secrets.token_hex(24)
        with Transaction().set_context(_direct_internal=_INTERNAL):
            self.__class__.write([self], {'task_token': token})
        due = min(self.next_attempt, self.expires)
        with Transaction().set_context(_direct_internal=None,
                queue_name=config.get('electronic_mail', 'queue_name', default='default'),
                queue_scheduled_at=max(due - datetime.utcnow(), timedelta())):
            self.__class__.__queue__._deliver([self], token)

    def _defer(self, detail):
        delay = min(1800 * (2 ** min(max(self.attempts - 1, 0), 4)), 21600)
        delay += random.SystemRandom().randint(0, 180)
        with Transaction().set_context(_direct_internal=_INTERNAL):
            self.__class__.write([self], {'state': 'pending', 'error': detail,
                'next_attempt': datetime.utcnow() + timedelta(seconds=delay),
                'history': self._history('deferred: ' + detail)})
        self._schedule()

    @classmethod
    def _deliver(cls, deliveries, token):
        """One recipient task, durable intent/result in independent transactions."""
        if not config.getboolean('database', 'production', default=False):
            raise UserError('Mail delivery is disabled outside production.')
        Mail = Pool().get('electronic.mail')
        table = cls.__table__()
        for record in deliveries:
            with Transaction().new_transaction() as step:
                # Only this recipient is serialized, never other recipients' results.
                step.connection.cursor().execute(*table.update(
                    [table.state], [table.state], where=table.id == record.id))
                delivery = cls(record.id)
                if delivery.task_token != token or delivery.state not in {'pending', 'sending'}:
                    continue
                if delivery.state == 'sending' or datetime.utcnow() >= delivery.expires:
                    state, detail = ('uncertain', 'Worker interrupted; acceptance is unknown') \
                        if delivery.state == 'sending' else ('failed', 'Delivery expired after five days')
                    with Transaction().set_context(_direct_internal=_INTERNAL):
                        cls.write([delivery], {'state': state, 'error': detail,
                            'history': delivery._history(detail)})
                    with Transaction().set_context(_direct_internal=None):
                        Mail.__queue__._update_delivery([delivery.mail])
                    step.commit()
                    continue
                mail = delivery.mail
                server = mail.delivery_server
                envelope, envid = mail.delivery_envelope, mail.delivery_token
                address = delivery.recipient
                settings = (server.direct_hostname, server.smtp_timeout, server.direct_require_tls)
                with Transaction().set_context(_direct_internal=_INTERNAL):
                    cls.write([delivery], {'attempts': delivery.attempts + 1})
                try:
                    if not server.direct_delivery or server.state != 'done':
                        raise ValueError('Direct delivery server is not confirmed')
                    if envelope.rsplit('@', 1)[1] != server.direct_domain:
                        raise ValueError('Sending domain changed; review the queued message')
                    with Transaction().set_context({'electronic.mail.mail_file': None}):
                        wire = bytes(Mail(mail.id).mail_file)
                    if not wire.startswith(b'DKIM-Signature:'):
                        # The only shared write is preparing the first signature.
                        # Subsequent attempts reuse the same immutable bytes.
                        mail_table = Mail.__table__()
                        step.connection.cursor().execute(*mail_table.update(
                            [mail_table.size], [mail_table.size], where=mail_table.id == mail.id))
                        with Transaction().set_context({'electronic.mail.mail_file': None}):
                            wire = bytes(Mail(mail.id).mail_file)
                        if not wire.startswith(b'DKIM-Signature:'):
                            path = config.get('smtp_dkim', server.direct_key)
                            if not path:
                                raise ValueError('DKIM key configuration is missing')
                            wire = transport.sign(wire, server.direct_domain,
                                server.direct_selector, path, transport.dns.resolver.Resolver())
                            with Transaction().set_context(_direct_internal=_INTERNAL):
                                Mail.write([mail], {'mail_file': wire, 'size': len(wire)})
                except (backend.DatabaseOperationalError, TransactionError):
                    raise
                except Exception as error:
                    delivery._defer(f'DKIM/configuration unavailable: {error}'[:1000])
                    step.commit()
                    continue
                with Transaction().set_context(_direct_internal=_INTERNAL):
                    cls.write([delivery], {'state': 'sending', 'started': datetime.utcnow()})
                step.commit()
            try:
                state, detail = transport.deliver(wire, envelope, address, *settings, envid=envid)
            except Exception as error:
                state, detail = 'uncertain', f'Unexpected transport failure: {error}'[:1000]
            with Transaction().new_transaction() as finish:
                finish.connection.cursor().execute(*table.update(
                    [table.state], [table.state], where=table.id == record.id))
                delivery = cls(record.id)
                if delivery.task_token != token:
                    continue
                if state == 'pending':
                    delivery._defer(detail)
                else:
                    with Transaction().set_context(_direct_internal=_INTERNAL):
                        cls.write([delivery], {'state': state, 'error': detail,
                            'next_attempt': None, 'history': delivery._history(state + ': ' + detail)})
                with Transaction().set_context(_direct_internal=None):
                    Mail.__queue__._update_delivery([delivery.mail])
                finish.commit()
