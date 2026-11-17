from datetime import datetime, timedelta
from email import policy
from email.message import EmailMessage
import os
from pathlib import Path
import smtplib
import ssl
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import dkim
import dns.resolver
from proteus import Model
from trytond.config import config
from trytond.exceptions import UserError
from trytond.pool import Pool
from trytond.tests.test_tryton import DB_NAME, drop_db
from trytond.tests.tools import activate_modules
from trytond.transaction import Transaction
from trytond.worker import run_task
from trytond.modules.smtp import mail as mail_module, direct_transport


class TestDirectDelivery(unittest.TestCase):
    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        activate_modules(['smtp', 'electronic_mail'])
        original_boolean = config.getboolean
        def worker_enabled(section, name, *args, **kwargs):
            if (section, name) in {('queue', 'worker'), ('database', 'production')}:
                return True
            return original_boolean(section, name, *args, **kwargs)
        worker_patch = patch.object(config, 'getboolean', side_effect=worker_enabled)
        worker_patch.start()
        self.addCleanup(worker_patch.stop)
        Server = Model.get('smtp.server')
        Mailbox = Model.get('electronic.mail.mailbox')
        Mail = Model.get('electronic.mail')
        mailbox = Mailbox(name='Outbox')
        mailbox.save()
        server = Server(name='Direct', direct_delivery=True,
            smtp_email='sender@example.com', direct_domain='example.com',
            direct_hostname='mail.example.com', direct_selector='mail2026',
            direct_key='test', direct_mailbox=mailbox)
        server.save()
        server.click('done')
        message = EmailMessage(policy=policy.SMTP)
        message['From'] = 'Sender <sender@example.com>'
        message['To'] = 'ok@recipient.test, retry@recipient.test'
        message['Bcc'] = 'hidden@recipient.test'
        message['Subject'] = 'Direct delivery'
        message.set_content('Bon dia, això és una prova.')

        def new_mail():
            mail = Mail(mailbox=mailbox, from_=str(message['From']),
                to=str(message['To']), bcc=str(message['Bcc']),
                subject='Direct delivery', body='Direct delivery', mail_file=message.as_bytes(),
                delivery_server=server)
            mail.save()
            return mail

        def run(method=None):
            # Drain due work with the real Tryton dispatcher, including dispatch
            # and aggregate tasks; future retries stay in ir.queue.
            executed = set()
            for _ in range(20):
                with Transaction(new=True).start(DB_NAME, 0):
                    Queue = Pool().get('ir.queue')
                    ids = [task.id for task in Queue.search([
                        ('finished_at', '=', None),
                        ['OR', ('scheduled_at', '=', None),
                            ('scheduled_at', '<=', datetime.utcnow())]])
                        if task.id not in executed
                        and (method is None or task.data['method'] == method)]
                if not ids:
                    return
                for identifier in ids:
                    run_task(DB_NAME, identifier)
                    executed.add(identifier)
            self.fail('Queue did not drain')

        def update(deliveries, values):
            with Transaction().start(DB_NAME, 0,
                    context={'_direct_internal': mail_module._INTERNAL}) as tx:
                Direct = Pool().get('electronic.mail.direct')
                Direct.write([Direct(d.id) for d in deliveries], values)
                tx.commit()

        mail = new_mail()
        mail.click('send_mail')
        self.assertFalse(mail.direct_deliveries)
        run('_send_mail')
        mail.reload()
        self.assertEqual(mail.delivery_state, 'queued')
        self.assertFalse(mail.flag_send)
        self.assertNotIn(b'Bcc:', mail.mail_file)
        self.assertIsNotNone(mail.message_id)
        self.assertIn('hidden@recipient.test', {d.recipient: d for d in mail.direct_deliveries})
        self.assertEqual(mail.size, len(mail.mail_file))
        frozen = mail.mail_file
        with self.assertRaises(UserError):
            mail.click('send_mail')
        self.assertEqual(mail.mail_file, frozen)
        with self.assertRaises(UserError):
            Mail._proxy.write([mail.id], {'mail_file': b'changed'}, {})
        with self.assertRaises(UserError):
            Mail._proxy.write([mail.id], {'delivery_state': 'sent'}, {})
        with self.assertRaises(UserError):
            with Transaction().start(DB_NAME, 0):
                Pool().get('electronic.mail').create([{
                    'mailbox': mailbox.id, 'delivery_token': 'forged'}])
        with self.assertRaises(UserError):
            Mail.delete([mail])
        # No outbound DNS or network at all outside production mode.
        with patch.object(mail_module.config, 'getboolean', return_value=False), patch.object(
                direct_transport, 'deliver') as delivery:
            with Transaction().start(DB_NAME, 0):
                with self.assertRaises(UserError):
                    Pool().get('electronic.mail.direct')._deliver([], '')
            delivery.assert_not_called()
        with TemporaryDirectory() as directory:
            key_path = Path(directory) / 'private.pem'
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            os.chmod(key_path, 0o600)
            _, public = direct_transport.load_key(key_path)
            resolver = MagicMock()
            resolver.resolve.return_value = [SimpleNamespace(strings=[public.encode()])]
            get = config.get
            getboolean = config.getboolean
            def setting(section, name, *args, **kwargs):
                if (section, name) == ('smtp_dkim', 'test'):
                    return str(key_path)
                return get(section, name, *args, **kwargs)
            def boolean(section, name, *args, **kwargs):
                if (section, name) == ('database', 'production'):
                    return True
                return getboolean(section, name, *args, **kwargs)
            calls = []
            def delivery(wire, sender, recipient, *args, **kwargs):
                self.assertTrue(dkim.verify(wire, dnsfunc=lambda *a, **kw: public.encode()))
                self.assertNotIn(b'Bcc:', wire)
                calls.append(recipient)
                return {
                    'ok@recipient.test': ('sent', '250 accepted'),
                    'retry@recipient.test': ('pending', '451 greylisted'),
                    'hidden@recipient.test': ('failed', '550 unknown user'),
                    }[recipient]
            with patch.object(config, 'get', side_effect=setting), patch.object(
                    config, 'getboolean', side_effect=boolean), patch.object(
                    direct_transport.dns.resolver, 'Resolver', return_value=resolver), patch.object(
                    direct_transport, 'deliver', side_effect=delivery):
                run()
            mail.reload()
            states = {d.recipient: d for d in mail.direct_deliveries}
            self.assertEqual(len(calls), 3)
            self.assertEqual(states['ok@recipient.test'].state, 'sent')
            self.assertEqual(states['hidden@recipient.test'].state, 'failed')
            self.assertEqual(states['retry@recipient.test'].state, 'pending')
            self.assertGreater(mail.delivery_due, datetime.utcnow() + timedelta(minutes=29))
            self.assertFalse(mail.flag_send)
            self.assertEqual(mail.delivery_state, 'queued')
            # Retry only the pending recipient; preserve exact signed bytes.
            signed = mail.mail_file
            self.assertTrue(signed.startswith(b'DKIM-Signature:'))
            self.assertEqual(mail.size, len(signed))
            update([states['retry@recipient.test']], {'next_attempt': datetime.utcnow()})
            with Transaction().start(DB_NAME, 0) as tx:
                Queue = Pool().get('ir.queue')
                Queue.write(Queue.search([('finished_at', '=', None)]),
                    {'scheduled_at': datetime.utcnow()})
                tx.commit()
            with patch.object(config, 'getboolean', side_effect=boolean), patch.object(
                    direct_transport, 'deliver', return_value=('sent', '250 accepted')) as send:
                run()
                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.args[0], signed)
                self.assertEqual(send.call_args.args[2], 'retry@recipient.test')
            mail.reload()
            self.assertEqual(mail.delivery_state, 'partial')
            self.assertFalse(mail.flag_send)
            # An aggregate-task rollback must not discard accepted recipients.
            mail.click('retry_direct')
            with patch.object(config, 'getboolean', side_effect=boolean), patch.object(
                    direct_transport, 'deliver', return_value=('sent', '250 accepted')) as send, \
                    patch.object(mail_module.Mail, '_update_delivery',
                        side_effect=RuntimeError('aggregate failed')):
                run()
                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.args[2], 'hidden@recipient.test')
            mail.reload()
            self.assertEqual(mail.delivery_state, 'sent')
            self.assertTrue(all(d.state == 'sent' for d in mail.direct_deliveries))
            self.assertFalse(mail.flag_send)
            with Transaction().start(DB_NAME, 0) as tx:
                ModelMail = Pool().get('electronic.mail')
                ModelMail.__queue__._update_delivery([ModelMail(mail.id)])
                tx.commit()
            with patch.object(direct_transport, 'deliver') as send:
                run()
                send.assert_not_called()
            mail.reload()
            self.assertTrue(mail.flag_send)
            # A later imported DSN is correlated by ENVID and exact recipient.
            report = (b'From: postmaster@recipient.test\r\n'
                b'To: sender@example.com\r\nSubject: Delivery status\r\n'
                b'MIME-Version: 1.0\r\n'
                b'Content-Type: multipart/report; report-type=delivery-status; boundary=dsn\r\n'
                b'\r\n--dsn\r\nContent-Type: text/plain\r\n\r\nFailed\r\n'
                b'--dsn\r\nContent-Type: message/delivery-status\r\n\r\n'
                b'Reporting-MTA: dns; recipient.test\r\nOriginal-Envelope-ID: '
                + mail.delivery_token.encode() + b'\r\n\r\n'
                b'Final-Recipient: rfc822; ok@recipient.test\r\nAction: failed\r\n'
                b'Status: 5.2.2\r\nDiagnostic-Code: smtp; Mailbox full\r\n\r\n--dsn--\r\n')
            with Transaction().start(DB_NAME, 0) as tx:
                Pool().get('electronic.mail').create_from_mail(
                    mail_module.BytesParser().parsebytes(report), mailbox.id)
                tx.commit()
            with patch.object(config, 'getboolean', side_effect=boolean):
                run()
            mail.reload()
            self.assertEqual(mail.delivery_state, 'partial')
            self.assertIn('Mailbox full', next(d.history for d in mail.direct_deliveries
                if d.recipient == 'ok@recipient.test'))
            # Unexpected transport failure becomes uncertain, without resending.
            crash = new_mail()
            crash.click('send_mail')
            with patch.object(config, 'get', side_effect=setting), patch.object(
                    config, 'getboolean', side_effect=boolean), patch.object(
                    direct_transport.dns.resolver, 'Resolver', return_value=resolver), patch.object(
                    direct_transport, 'deliver', side_effect=RuntimeError('connection lost')):
                run()
            crash.reload()
            self.assertEqual(crash.delivery_state, 'uncertain')
            self.assertFalse(crash.flag_send)
            crash.click('cancel_direct')
            self.assertEqual(crash.delivery_state, 'cancelled')
            # A killed worker leaves an unfinished Tryton task. Replaying that
            # same task must not resend the recipient whose acceptance is unknown.
            interrupted = new_mail()
            interrupted.click('send_mail')
            with patch.object(config, 'get', side_effect=setting), patch.object(
                    config, 'getboolean', side_effect=boolean), patch.object(
                    direct_transport.dns.resolver, 'Resolver', return_value=resolver), patch.object(
                    direct_transport, 'deliver', side_effect=[
                        ('sent', '250 accepted'), SystemExit('worker killed')]):
                with self.assertRaises(SystemExit):
                    run()
            with patch.object(config, 'getboolean', side_effect=boolean), patch.object(
                    direct_transport, 'deliver', return_value=('sent', '250 accepted')) as send:
                run()
                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.args[2], 'hidden@recipient.test')
            interrupted.reload()
            self.assertEqual(interrupted.delivery_state, 'uncertain')
            interrupted.click('cancel_direct')
            # Expired recipients are failed without network activity.
            expired = new_mail()
            expired.click('send_mail')
            run('_send_mail')
            expired.reload()
            update(expired.direct_deliveries, {'expires': datetime.utcnow() - timedelta(days=1)})
            with patch.object(config, 'getboolean', side_effect=boolean), patch.object(
                    direct_transport, 'deliver') as send:
                run()
                send.assert_not_called()
            expired.reload()
            self.assertEqual(expired.delivery_state, 'failed')
            # DKIM misconfiguration defers, never sends unsigned.
            broken = new_mail()
            broken.click('send_mail')
            with patch.object(config, 'getboolean', side_effect=boolean), patch.object(
                    direct_transport, 'deliver') as send:
                run()
                send.assert_not_called()
            broken.reload()
            self.assertEqual(broken.delivery_state, 'queued')
            self.assertTrue(all('DKIM/configuration unavailable' in d.error
                for d in broken.direct_deliveries))

        # MX priority, equal-priority hosts and safe numeric address pinning.
        resolver = MagicMock()
        def resolve(name, kind, **kwargs):
            if kind == 'MX':
                return [SimpleNamespace(preference=20, exchange='backup.example.com.'),
                    SimpleNamespace(preference=10, exchange='primary.example.com.')]
            if kind == 'AAAA':
                raise dns.resolver.NoAnswer
            return ['93.184.216.34']
        resolver.resolve.side_effect = resolve
        candidates = list(direct_transport.mx_addresses('recipient.test', resolver))
        self.assertEqual([h for h, ip in candidates],
            ['primary.example.com', 'backup.example.com'])
        clients = []
        def smtp_factory(host, address, helo, timeout):
            if host == 'primary.example.com':
                raise ConnectionRefusedError('primary offline')
            client = MagicMock()
            client.ehlo.return_value = (250, b'hello')
            client.has_extn.side_effect = lambda name: name == 'starttls'
            client.mail.return_value = (250, b'ok')
            client.rcpt.return_value = (250, b'ok')
            client.data.return_value = (250, b'accepted')
            clients.append(client)
            return client
        result, _ = direct_transport.deliver(b'From: sender@example.com\r\n\r\nTest',
            'sender@example.com', 'ok@recipient.test', 'mail.example.com',
            resolver=resolver, smtp_factory=smtp_factory)
        self.assertEqual(result, 'sent')
        context = clients[0].starttls.call_args.kwargs['context']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        client = clients[0]
        client.data.side_effect = smtplib.SMTPServerDisconnected('lost DATA reply')
        result, _ = direct_transport.deliver(b'test', 'sender@example.com',
            'ok@recipient.test', 'mail.example.com', resolver=resolver,
            smtp_factory=lambda *args: client)
        self.assertEqual(result, 'uncertain')
        client.data.side_effect = smtplib.SMTPDataError(451, b'try later')
        result, _ = direct_transport.deliver(b'test', 'sender@example.com',
            'ok@recipient.test', 'mail.example.com', resolver=resolver,
            smtp_factory=lambda *args: client)
        self.assertEqual(result, 'pending')
        client.data.side_effect = smtplib.SMTPDataError(550, b'rejected')
        result, _ = direct_transport.deliver(b'test', 'sender@example.com',
            'ok@recipient.test', 'mail.example.com', resolver=resolver,
            smtp_factory=lambda *args: client)
        self.assertEqual(result, 'failed')
        resolver.resolve.side_effect = None
        resolver.resolve.return_value = [SimpleNamespace(preference=0, exchange='.')]
        result, _ = direct_transport.deliver(b'test', 'sender@example.com',
            'ok@recipient.test', 'mail.example.com', resolver=resolver)
        self.assertEqual(result, 'failed')
        resolver.resolve.side_effect = dns.resolver.LifetimeTimeout
        result, _ = direct_transport.deliver(b'test', 'sender@example.com',
            'ok@recipient.test', 'mail.example.com', resolver=resolver)
        self.assertEqual(result, 'pending')
        resolver.resolve.side_effect = lambda name, kind, **kw: (
            [SimpleNamespace(preference=0, exchange='internal.example.com')]
            if kind == 'MX' else ['127.0.0.1'])
        with self.assertRaises(ValueError):
            list(direct_transport.mx_addresses('recipient.test', resolver))
        with self.assertRaises(ValueError):
            direct_transport.prepare(message.as_bytes(), 'attacker@elsewhere.test',
                ['ok@recipient.test'], 'example.com')
