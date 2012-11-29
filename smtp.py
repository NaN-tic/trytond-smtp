#This file is part smtp module for Tryton.
#The COPYRIGHT file at the top level of this repository contains
#the full copyright notices and license terms.
from trytond.model import ModelView, ModelSQL, fields
from trytond.pool import Pool
from trytond.pyson import Eval

import smtplib

__all__ = ['SmtpServer']

class SmtpServer(ModelSQL, ModelView):
    'SMTP Servers'
    __name__ = 'smtp.server'
    name = fields.Char('Name', required=True)
    smtp_server = fields.Char('Server', required=True,
        states={
            'readonly': (Eval('state') != 'draft'),
        }, depends=['state'])
    smtp_port  = fields.Integer('Port', required=True,
        states={
            'readonly': (Eval('state') != 'draft'),
        }, depends=['state'])
    smtp_ssl = fields.Boolean('SSL',
        states={
            'readonly': (Eval('state') != 'draft'),
        }, depends=['state'])
    smtp_tls = fields.Boolean('TLS',
        states={
            'readonly': (Eval('state') != 'draft'),
        }, depends=['state'])
    smtp_user = fields.Char('User',
        states={
            'readonly': (Eval('state') != 'draft'),
        }, depends=['state'])
    smtp_password = fields.Char('Password',
        states={
            'readonly': (Eval('state') != 'draft'),
        }, depends=['state'])
    smtp_use_email = fields.Boolean('Use email',
        states={
            'readonly': (Eval('state') != 'draft'),
        }, depends=['state'], help='Force to send emails using this email')
    smtp_email = fields.Char('Email', required=True,
        states={
            'readonly': (Eval('state') != 'draft'),
        }, depends=['state'], help='Default From (if active this option) and Reply Email')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('done', 'Done'),
    ], 'State', readonly=True, required=True)
    default = fields.Boolean('Default')

    @classmethod
    def __setup__(cls):
        super(SmtpServer, cls).__setup__()
        cls._error_messages.update({
            'smtp_successful': 'SMTP Test Connection Was Successful',
            'smtp_test_details': 'SMTP Test Connection Details:\n%s',
            'smtp_error': 'SMTP Test Connection Failed.',
        })
        cls._buttons.update({
            'get_smtp_test': {},
            'draft': {
                'invisible': Eval('state') == 'draft',
                },
            'done': {
                'invisible': Eval('state') == 'done',
                },
            })

    @staticmethod
    def default_active():
        return True

    @staticmethod
    def default_default():
        return True

    @staticmethod
    def default_ssl():
        return True

    @staticmethod
    def default_tls():
        return True

    @staticmethod
    def default_state():
        return 'draft'

    @staticmethod
    def default_smtp_port():
        return 465

    @classmethod
    @ModelView.button
    def draft(cls, servers):
        draft = []
        for server in servers:
            draft.append(server)
        cls.write(draft, {
            'state': 'draft',
            })

    @classmethod
    @ModelView.button
    def done(cls, servers):
        done = []
        for server in servers:
            done.append(server)
        cls.write(done, {
            'state': 'done',
            })

    @classmethod
    @ModelView.button
    def get_smtp_test(cls, servers):
        """Checks SMTP credentials and confirms if outgoing connection works"""
        for server in servers:
            try:
                cls.get_smtp_server(server)
            except Exception, message:
                cls.raise_user_error('smtp_test_details', message)
            except:
                cls.raise_user_error('smtp_error')
            cls.raise_user_error('smtp_successful')

    @staticmethod
    def get_smtp_server(server):
        """
        Instanciate, configure and return a SMTP or SMTP_SSL instance from
        smtplib.
        :return: A SMTP instance. The quit() method must be call when all
        the calls to sendmail() have been made.
        """
        if server.smtp_ssl:
            smtp_server = smtplib.SMTP_SSL(server.smtp_server, server.smtp_port)
        else:
            smtp_server = smtplib.SMTP(server.smtp_server, server.smtp_port)

        if server.smtp_tls:
            smtp_server.starttls()

        if server.smtp_user and server.smtp_password:
            smtp_server.login(server.smtp_user, server.smtp_password)

        return smtp_server
