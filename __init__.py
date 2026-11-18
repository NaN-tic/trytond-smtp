# This file is part smtp module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from trytond.pool import Pool
from . import smtp
from . import mail


def register():
    Pool.register(
        smtp.SmtpServer,
        smtp.SmtpServerModel,
        module='smtp', type_='model')

    Pool.register(mail.Server, mail.Mail, mail.Direct,
        module='smtp', type_='model', depends=['electronic_mail'])
