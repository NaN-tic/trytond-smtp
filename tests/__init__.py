# This file is part smtp module for Tryton.  The COPYRIGHT file at the top
# level of this repository contains the full copyright notices and license
# terms.
try:
    from trytond.modules.smtp.tests.test_smtp import suite
except ImportError:
    from .test_smtp import suite

__all__ = ['suite']
