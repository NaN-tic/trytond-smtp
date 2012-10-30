SMTP Module
###########

The smtp module add smtp servers to anothers modules use it and send emails.

Example send email:

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
