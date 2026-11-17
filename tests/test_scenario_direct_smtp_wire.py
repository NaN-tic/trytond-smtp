from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
import os
from pathlib import Path
import socket
import socketserver
import ssl
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import dkim
from trytond.tests.test_tryton import drop_db
from trytond.modules.smtp import direct_transport as transport


class TestDirectSMTPWire(unittest.TestCase):
    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        with TemporaryDirectory() as directory:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            private_path = Path(directory) / 'key.pem'
            private_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            os.chmod(private_path, 0o600)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'mx.example.test')])
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
                .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([
                    x509.DNSName('mx.example.test')]), critical=False)
                .sign(key, hashes.SHA256()))
            cert_path = Path(directory) / 'cert.pem'
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(cert_path, private_path)
            client_context = ssl.create_default_context(cafile=cert_path)
            commands = []
            messages = []

            class Receiver(socketserver.StreamRequestHandler):
                def handle(self):
                    self.wfile.write(b'220 mx.example.test ESMTP\r\n')
                    encrypted = False
                    while line := self.rfile.readline():
                        commands.append(line)
                        command = line.split()[0].upper()
                        if command == b'EHLO':
                            self.wfile.write(b'250-mx.example.test\r\n')
                            if not encrypted:
                                self.wfile.write(b'250-STARTTLS\r\n')
                            self.wfile.write(b'250-DSN\r\n250 SIZE 25000000\r\n')
                        elif command == b'STARTTLS':
                            self.wfile.write(b'220 Start TLS\r\n')
                            self.rfile.close()
                            self.wfile.close()
                            try:
                                self.connection = server_context.wrap_socket(
                                    self.connection, server_side=True)
                            except ssl.SSLError:
                                return
                            self.rfile = self.connection.makefile('rb')
                            self.wfile = self.connection.makefile('wb', buffering=0)
                            encrypted = True
                        elif command in (b'MAIL', b'RCPT'):
                            self.wfile.write(b'250 OK\r\n' if encrypted else b'530 TLS required\r\n')
                        elif command == b'DATA':
                            self.wfile.write(b'354 Continue\r\n')
                            chunks = []
                            while (line := self.rfile.readline()) != b'.\r\n':
                                if not line:
                                    return
                                chunks.append(line[1:] if line.startswith(b'..') else line)
                            messages.append(b''.join(chunks))
                            self.wfile.write(b'250 Accepted\r\n')
                        else:
                            self.wfile.write(b'250 OK\r\n')

            with socketserver.TCPServer(('127.0.0.1', 0), Receiver) as receiver:
                thread = threading.Thread(target=receiver.serve_forever, daemon=True)
                thread.start()
                _, public = transport.load_key(private_path)
                message = EmailMessage(policy=policy.SMTP)
                message['From'] = 'sender@example.com'
                message['To'] = 'recipient@example.net'
                message['Bcc'] = 'hidden@example.net'
                message.set_content('.Leading dot\nUnicode català\n')
                wire, sender, recipients, _ = transport.prepare(message.as_bytes(),
                    'sender@example.com', ['recipient@example.net'], 'example.com')
                resolver = SimpleNamespace(resolve=lambda *a, **kw: [
                    SimpleNamespace(strings=[public.encode()])])
                signed = transport.sign(wire, 'example.com', 'test', str(private_path), resolver)
                def connect(instance, host, port, timeout):
                    return socket.create_connection(receiver.server_address, timeout)
                try:
                    with patch.object(transport, 'mx_addresses', return_value=iter([
                            ('mx.example.test', '93.184.216.34')])), patch.object(
                            transport.PinnedSMTP, '_get_socket', connect), patch.object(
                            transport.ssl, 'create_default_context', return_value=client_context):
                        state, detail = transport.deliver(signed, sender, recipients[0],
                            'sender.example.com', envid='a' * 48)
                    self.assertEqual(state, 'sent', detail)
                    self.assertEqual(len(messages), 1)
                    self.assertTrue(dkim.verify(messages[0], dnsfunc=lambda *a, **kw: public.encode()))
                    self.assertNotIn(b'Bcc:', messages[0])
                    self.assertIn(b'.Leading dot', messages[0])
                    self.assertTrue(any(b'ENVID=' + b'a' * 48 in c for c in commands))
                    self.assertTrue(any(b'NOTIFY=FAILURE,DELAY' in c for c in commands))
                    with patch.object(transport, 'mx_addresses', return_value=iter([
                            ('wrong.example.test', '93.184.216.34')])), patch.object(
                            transport.PinnedSMTP, '_get_socket', connect), patch.object(
                            transport.ssl, 'create_default_context', return_value=client_context):
                        state, detail = transport.deliver(signed, sender, recipients[0],
                            'sender.example.com')
                    self.assertEqual(state, 'pending')
                    self.assertIn('CERTIFICATE_VERIFY_FAILED', detail)
                    self.assertEqual(len(messages), 1)
                finally:
                    receiver.shutdown()
                    thread.join(timeout=5)
