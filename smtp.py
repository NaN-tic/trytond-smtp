#This file is part smtp module for Tryton.
#The COPYRIGHT file at the top level of this repository contains
#the full copyright notices and license terms.
from trytond.model import ModelView, ModelSQL, fields
from trytond.pool import Pool
from trytond.pyson import Eval

import smtplib

__all__ = ['SmtpServer', 'SmtpServerModel']


class SmtpServer(ModelSQL, ModelView):
    'SMTP Servers'
    __name__ = 'smtp.server'
    name = fields.Char('Name', required=True)
    smtp_server = fields.Char('Server', required=True,
        states={
            'readonly': (Eval('state') != 'draft'),
        }, depends=['state'])
    smtp_port = fields.Integer('Port', required=True,
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
        }, depends=['state'],
        help='Default From (if active this option) and Reply Email')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('done', 'Done'),
    ], 'State', readonly=True, required=True)
    default = fields.Boolean('Default')
    models = fields.Many2Many('smtp.server-ir.model',
            'server', 'model', 'Models',
        states={
            'readonly': Eval('state').in_(['done']),
            },
        depends=['state'])

    @classmethod
    def __setup__(cls):
        super(SmtpServer, cls).__setup__()
        cls._error_messages.update({
            'smtp_successful': 'SMTP Test Connection Was Successful',
            'smtp_test_details': 'SMTP Test Connection Details:\n%s',
            'smtp_error': 'SMTP Test Connection Failed.',
            'server_model_not_found': 'There are not SMTP server related '
                'at model %s',
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

    @classmethod
    def check_xml_record(cls, records, values):
        return True

    @staticmethod
    def default_active():
        return True

    @staticmethod
    def default_default():
        return True

    @staticmethod
    def default_smtp_ssl():
        return True

    @staticmethod
    def default_smtp_port():
        return 465

    @staticmethod
    def default_state():
        return 'draft'

    @classmethod
    @ModelView.button
    def draft(cls, servers):
        cls.write(servers, {
            'state': 'draft',
            })

    @classmethod
    @ModelView.button
    def done(cls, servers):
        cls.write(servers, {
            'state': 'done',
            })

    @classmethod
    @ModelView.button
    def get_smtp_test(cls, servers):
        """Checks SMTP credentials and confirms if outgoing connection works"""
        for server in servers:
            try:
                server.get_smtp_server()
            except Exception, message:
                cls.raise_user_error('smtp_test_details', message)
            except:
                cls.raise_user_error('smtp_error')
            cls.raise_user_error('smtp_successful')

    def get_smtp_server(self):
        """
        Instanciate, configure and return a SMTP or SMTP_SSL instance from
        smtplib.
        :return: A SMTP instance. The quit() method must be call when all
        the calls to sendmail() have been made.
        """
        if self.smtp_ssl:
            smtp_server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port)
        else:
            smtp_server = smtplib.SMTP(self.smtp_server, self.smtp_port)

        if self.smtp_tls:
            smtp_server.starttls()

        if self.smtp_user and self.smtp_password:
            smtp_server.login(self.smtp_user.encode('UTF-8'),
                self.smtp_password.encode('UTF-8'))

        return smtp_server

    @classmethod
    def get_smtp_server_from_model(self, model):
        """
        Return Server from Models
        :param model: str Model name
        return object server
        """
        model = Pool().get('ir.model').search([('model', '=', model)])[0]
        servers = Pool().get('smtp.server-ir.model').search([
            ('model', '=', model)], limit=1)
        if not servers:
            self.raise_user_error('server_model_not_found', model.name)
        return servers[0].server


class SmtpServerModel(ModelSQL):
    'SMTP Server - Model'
    __name__ = 'smtp.server-ir.model'
    _table = 'smtp_server_ir_model'

    server = fields.Many2One('smtp.server', 'Server', ondelete='CASCADE',
            select=True, required=True)
    model = fields.Many2One('ir.model', 'Model', ondelete='RESTRICT',
            select=True, required=True)
