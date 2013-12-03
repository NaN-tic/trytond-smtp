=====================
SMTP. Envío de correo
=====================

Este módulo permite dar de alta servidores para el envío de correos
electrónicos mediante el protocolo SMTP. 

Este módulos sirve de base para que otros módulos puedan enviar correos
electrónicos, como por ejemplo el módulo **Plantillas correo electrónico**.

.. inheritref:: smtp/smtp:section:grupos

Grupo de usuarios
=================

Sólo los usuarios del grupo **Administrador SMTP** podrán gestionar cuentas de
servidores SMTP.

.. inheritref:: smtp/smtp:section:uso

Utilización
===========

* Para poder enviar correos electrónicos desde una cuenta determinada, primero
  se debe configurar y aprobar la cuenta en el menú |menu_server_form|.
* Marque al menos una cuenta como cuenta *Por defecto* para que pueda ser
  utilizada por planificadores.

.. |menu_server_form| tryref:: smtp.menu_server_form/complete_name

.. inheritref:: smtp/smtp:section:api

API
===

.. code:: python

    SMTP = Pool().get('smtp.server')
    
    servers = SMTP.search([('state','=','done'),('default','=',True)])
    if not len(servers)>0:
        self.raise_user_error('smtp_server_default')
    server = servers[0]
    
    try:
        server = SMTP.get_smtp_server(server)
        server.sendmail('from', 'to', 'body')
        server.quit()
    except:
        self.raise_user_error('smtp_error')
    return True
