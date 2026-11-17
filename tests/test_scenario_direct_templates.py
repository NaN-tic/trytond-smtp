from datetime import datetime, timedelta
from email import policy
from email.message import EmailMessage
import unittest
from unittest.mock import patch

from proteus import Model
from trytond.modules.company.tests.tools import create_company, get_company
from trytond.pool import Pool
from trytond.config import config
from trytond.tests.test_tryton import DB_NAME, drop_db
from trytond.tests.tools import activate_modules
from trytond.transaction import Transaction
from trytond.worker import run_task


class TestDirectTemplates(unittest.TestCase):
    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        cfg = activate_modules('electronic_mail_template')
        original_boolean = config.getboolean
        def worker_enabled(section, name, *args, **kwargs):
            if (section, name) in {('queue', 'worker'), ('database', 'production')}:
                return True
            return original_boolean(section, name, *args, **kwargs)
        worker_patch = patch.object(config, 'getboolean', side_effect=worker_enabled)
        worker_patch.start()
        self.addCleanup(worker_patch.stop)
        create_company(config=cfg)
        company = get_company()
        Mailbox = Model.get('electronic.mail.mailbox')
        Server = Model.get('smtp.server')
        mailbox = Mailbox(name='Outbox', company=company)
        mailbox.save()
        server = Server(name='Direct', direct_delivery=True,
            smtp_email='sender@example.com', direct_domain='example.com',
            direct_hostname='mail.example.com', direct_selector='mail2026',
            direct_key='test', direct_mailbox=mailbox)
        server.save()
        server.click('done')
        message = EmailMessage(policy=policy.SMTP)
        message['From'] = 'sender@example.com'
        message['To'] = 'receiver@example.net'
        message.set_content('Template flow')
        with Transaction().start(DB_NAME, 0, context={'company': company.id},
                _lock_tables=['electronic_mail_configuration', 'electronic_mail']) as tx:
            pool = Pool()
            Configuration = pool.get('electronic.mail.configuration')
            CompanyConfiguration = pool.get('electronic.mail.configuration.company')
            CompanyConfiguration.create([{'company': company.id,
                'draft': mailbox.id, 'outbox': mailbox.id,
                'sent': mailbox.id, 'error': mailbox.id}])
            Configuration.create([{'send_email_after': timedelta(minutes=5)}])
            Mail = pool.get('electronic.mail')
            mail = Mail.create_from_mail(message, mailbox.id)
            self.assertTrue(config.getboolean('database', 'production'))
            self.assertFalse(mail.flag_send)
            self.assertEqual(Mail._get_sender_and_recipients(mail),
                ('sender@example.com', ['receiver@example.net']))
            with patch('trytond.modules.smtp.direct_transport.deliver') as send:
                Mail.send_mail([mail], server=pool.get('smtp.server')(server.id),
                    sender='sender@example.com', recipients=['receiver@example.net'])
                send.assert_not_called()
            self.assertEqual(mail.delivery_state, '')
            self.assertFalse(mail.direct_deliveries)
            Queue = pool.get('ir.queue')
            tasks = Queue.search([('finished_at', '=', None)])
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].data['method'], '_send_mail')
            task_id = tasks[0].id
            self.assertGreater(tasks[0].scheduled_at,
                datetime.utcnow() + timedelta(minutes=4))
            self.assertLess(tasks[0].scheduled_at,
                datetime.utcnow() + timedelta(minutes=6))
            self.assertFalse(mail.flag_send)
            self.assertEqual(Mail.search_count([]), 1)
            identifier = mail.id
            tx.commit()
        # Execute only dispatch: no network, one child/task, no second grace delay.
        with patch('trytond.modules.smtp.direct_transport.deliver') as send:
            run_task(DB_NAME, task_id)
            send.assert_not_called()
        # Queueing the same record through the template worker is idempotent.
        with Transaction().start(DB_NAME, 0, context={'company': company.id},
                _lock_records={'electronic_mail': [identifier]}) as tx:
            Mail = Pool().get('electronic.mail')
            self.assertEqual(Mail(identifier).delivery_state, 'queued')
            self.assertEqual(len(Mail(identifier).direct_deliveries), 1)
            self.assertLess(Mail(identifier).direct_deliveries[0].next_attempt,
                datetime.utcnow() + timedelta(seconds=5))
            Queue = Pool().get('ir.queue')
            before = Queue.search_count([])
            Mail._send_mail([Mail(identifier)])
            self.assertEqual(Queue.search_count([]), before)
            self.assertEqual(Mail.search_count([]), 1)
            self.assertFalse(Mail(identifier).flag_send)
            tx.commit()
        # The same entry point dispatches conventional SMTP only in the worker.
        relay = Server(name='Relay', smtp_server='smtp.example.com',
            smtp_email='sender@example.com', default=False)
        relay.save()
        relay.click('done')
        with Transaction().start(DB_NAME, 0, context={'company': company.id},
                _lock_tables=['electronic_mail']) as tx:
            Mail = Pool().get('electronic.mail')
            relay_mail = Mail.create_from_mail(message, mailbox.id)
            with patch('trytond.modules.smtp.smtp.SmtpServer.send_mail') as send:
                Mail.send_mail([relay_mail], server=Pool().get('smtp.server')(relay.id),
                    sender='sender@example.com', recipients=['receiver@example.net'])
                send.assert_not_called()
            Queue = Pool().get('ir.queue')
            task, = [q for q in Queue.search([('finished_at', '=', None)])
                if q.data['method'] == '_send_mail']
            relay_task = task.id
            relay_id = relay_mail.id
            tx.commit()
        with patch('trytond.modules.smtp.smtp.SmtpServer.send_mail', return_value=True) as send:
            run_task(DB_NAME, relay_task)
            send.assert_called_once()
        with Transaction().start(DB_NAME, 0):
            Mail = Pool().get('electronic.mail')
            self.assertTrue(Mail(relay_id).flag_send)
            self.assertFalse(Mail(relay_id).direct_deliveries)
