#This file is part helloword module for Tryton.
#The COPYRIGHT file at the top level of this repository contains
#the full copyright notices and license terms.
from trytond.pool import Pool
from .smtp import *


def register():
    Pool.register(
        SmtpServer,
        SmtpServerModel,
        module='smtp', type_='model')
