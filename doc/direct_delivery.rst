Lliurament directe amb electronic.mail
#####################################

Activació i abast
=================

Activa ``smtp`` i ``electronic_mail`` i actualitza ``smtp``. La dependència
és opcional; no hi ha cap dependència inversa ni circular. El mode SMTP
convencional continua disponible. No cal Postfix, Dovecot, un servei extern
ni noves credencials SMTP per al mode directe.

Cada registre ``electronic.mail`` és un element de cua. El registre conté
el missatge de transport al camp ``mail_file``. Cada destinatari té un registre
``electronic.mail.direct`` amb un Many2One al correu, estat, intents, dates,
error i historial propis; no es desen els estats en un JSON compartit. En encuar-lo, ``mail_file`` es reemplaça pel missatge preparat,
sense Bcc; abans del primer intent s’hi desa la versió signada amb DKIM.
Els reintents reutilitzen aquests mateixos bytes. Els destinataris Bcc es
conserven a les dades d’enviament; no es conserva una còpia binària de l’original. No es fan enviaments dins de la transacció
que crea el correu. Els workers de Tryton executen les tasques d’``ir.queue`` després del commit,
amb ``[database] production = True`` i ``[queue] worker = True``.
No hi ha un cron d’enviament ni reserves pròpies de treballadors.
Cal executar ``trytond-worker`` i mantenir el cron estàndard de recuperació
de tasques interrompudes d’``ir.queue``.

Configuració
============

Cal una IP pública adequada per enviar correu, sortida TCP/25, A/AAAA i PTR
coherents amb el nom EHLO públic i SPF que autoritzi la IP real. No publiquis
AAAA si la sortida IPv6 no està preparada. DKIM no garanteix la reputació
ni el lliurament a la safata d'entrada.

Genera una clau RSA de 2048 bits o superior fora de la base de dades::

    openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out /secure/mail.pem
    chmod 600 /secure/mail.pem

L'usuari que executa Tryton ha de poder llegir-la. No pot ser accessible
per qualsevol usuari ni modificable pel grup. Configura ``trytond.conf``::

    [smtp_dkim]
    customer = /secure/mail.pem

Al servidor SMTP, marca ``Deliver Directly to MX`` i indica el domini
remitent, el nom públic del servidor, el selector DKIM, el nom de configuració
``customer`` i la bústia de sortida. El domini i el nom públic s'han d'indicar
en ASCII minúscul (punycode si cal). El camp ``DKIM DNS Record`` mostra el TXT
que s'ha de publicar. Conserva els SPF i DMARC existents i adapta'ls a aquesta
sortida. La publicació de DKIM es verifica abans de la primera entrega; no
s'envia mai sense signatura. En renovar claus, conserva el TXT del selector
antic mentre hi hagi missatges signats a la cua.

``Require Verified TLS`` està activat per defecte. Valida la cadena i el nom
del certificat de l'MX; una fallada de TLS provoca un reintent, mai una baixada
a text pla. Si es desmarca, només es permet text pla quan l'MX no anuncia
STARTTLS. No s'implementen DANE ni descobriment de polítiques MTA-STS; en entorns
que els requereixin, utilitza un MTA especialitzat.

Ús
===

El mètode compartit ``electronic.mail.send_mail`` i el botó ``Send Email``
els proporciona ``smtp`` quan ``electronic_mail`` està activat. Respecta
``send_email_after`` una sola vegada i encua sempre ``_send_mail``. Aquest
mètode s’executa al worker: envia per SMTP convencional o crea els registres
``electronic.mail.direct`` i encua el seu mètode ``_deliver`` per destinatari.
Les plantilles només personalitzen la selecció de servidor i bústia.
Pots seleccionar el servidor de lliurament al correu i prémer ``Send Email``;
no hi ha una acció separada per al transport directe. Els fluxos d'electronic_mail_template i
electronic_mail_activity també conserven el registre existent i l'encuen
quan el seu servidor utilitza el mode directe. Les integracions Python utilitzen la mateixa API per als dos transports::

    Mail.send_mail([mail])
    # Si cal escollir servidor i destinataris d’envelope:
    Mail.send_mail([mail], server=server, sender=sender, recipients=recipients)

El mòdul que ho crida no consulta ``direct_delivery`` ni modifica ``flag_send``.
La crida encua el correu; el worker gestiona la marca quan l’enviament és acceptat.
En SMTP convencional, es marca després de la crida al servidor; en directe,
quan tots els destinataris han estat acceptats. Un error no es marca com a èxit.
Si s’utilitza l’API de baix nivell ``server.send_mail(..., mail=mail)``, també
es delega al mateix mètode del correu, independentment del transport.
Els fluxos que utilitzen directament ``trytond.sendmail`` (com el client web
electronic_mail_client) mantenen el seu transport propi.

Els camps i el fitxer del missatge encuat són immutables. Per canviar-ne el
contingut, crea un altre registre. L'encuament intern d'un mateix registre és
idempotent; no es dedupliquen dos registres diferents amb el mateix Message-ID.

Lliurament i recuperació
=======================

* Consulta MX per prioritat creixent, amb ordre aleatori entre iguals; prova
  IPv6/IPv4 i altres MX en errors de connexió o temporals.
* Sense MX, prova A/AAAA del domini. Null MX i NXDOMAIN són errors definitius;
  timeout/SERVFAIL són temporals. No es connecta a IP privades, loopback ni
  altres adreces no globals; es fixa la IP resolta per evitar una segona
  resolució durant la connexió.
* Genera Date i Message-ID si falten, elimina Bcc del transport i comprova
  tant el remitent d'envelope com From i Sender contra el domini configurat.
  Converteix MIME a una representació de 7 bits i signa els bytes definitius.
* Manté resultats i reintents per destinatari. El límit és 100 destinataris i
  25 MB per missatge; no s'accepten adreces d'envelope SMTPUTF8 ni Resent-*.
* Reintenta errors temporals als 30 minuts, 1 hora, 2 hores, etc., fins a un
  màxim de 6 hores entre intents, amb un petit retard aleatori. Caduca als
  cinc dies. No reintenta automàticament errors definitius.
* Cada destinatari té una tasca d’``ir.queue``. Els errors temporals creen una
  tasca programada per al següent intent, atòmicament amb l’estat del destinatari.
  Tryton controla l’exclusió i la recuperació de tasques interrompudes. Un
  identificador d’intent invalida tasques antigues després d’una cancel·lació
  o reintent manual. Cada worker actualitza només el registre del destinatari.
  La primera signatura DKIM es desa coordinant una única escriptura compartida
  a ``mail_file``; no es mantenen bloquejos durant la xarxa.
  Cada intent de xarxa té un pressupost de cinc minuts.
* Abans de la xarxa, es confirma l'estat «sending» a la base de dades.
  Després de DATA, un tall de connexió o una caiguda del procés deixa el
  destinatari en «Needs Review»: no es pot garantir exactament una entrega
  amb SMTP. El reintent explícit pot generar un duplicat.
* «Accepted by MX» significa que tots els destinataris han estat acceptats
  pels seus servidors. No significa que l'usuari final l'hagi llegit ni que
  hagi arribat a la safata d'entrada. ``flag_send`` només es marca en aquest
  estat. L’estat global es calcula a partir dels destinataris; una tasca
  separada actualitza ``flag_send``. Si aquesta tasca falla, els resultats ja
  confirmats dels destinataris es conserven. Les acceptacions parcials es
  mostren per separat.
* Es demanen DSN de fallada i retard quan el destinatari anuncia aquesta
  extensió. S'utilitza un ENVID aleatori per correlacionar-los. Si els avisos
  es reben i s'importen amb ``electronic.mail.create_from_mail``, una tasca d’``ir.queue``
  actualitza el destinatari exacte i l'historial. Cal tenir recepció operativa
  a l'adreça remitent. Els avisos sense ENVID reconegut queden com a correu
  entrant normal; mai no s'infereix una fallada pel text de l'assumpte.

A ``SMTP / Direct Mail Outbox`` es poden consultar dates, estat, destinataris
amb error i historial. ``Cancel Pending Deliveries`` cancel·la allò pendent;
no retira missatges ja acceptats. ``Retry Unsent / Uncertain Recipients``
reobre només els destinataris no acceptats, amb un termini nou de cinc dies.
Per als incerts cal revisar abans els registres del destinatari, si és possible.
Les accions manuals no bloquegen les tasques en execució. Si coincideixen
amb un enviament, poden provocar un duplicat o no arribar a cancel·lar-lo. No es poden eliminar correus pendents o incerts sense resoldre'ls.

Conserva còpies de seguretat de la base de dades, el filestore i les claus.
No executis una còpia de producció amb ``production=True``: les còpies no
poden saber quins correus ha enviat la instal·lació original després del backup.
La concurrència la determina el nombre de workers; no hi ha un límit global
per proveïdor destinatari. Pots aïllar la feina amb ``[electronic_mail]
queue_name = mail`` i workers dedicats. Dimensiona el volum i monitora la cua
i la reputació. Els errors inesperats del worker es revisen amb les eines
estàndard de tasques de Tryton; un intent interromput mai no es reenvia
automàticament si l’acceptació és desconeguda.

Proves
======

Els escenaris ``direct_delivery`` i ``direct_smtp_wire`` cobreixen la cua,
DKIM, Bcc, acceptació parcial, reintents, caducitat, DSN, interrupció del
treballador, execució real amb ``run_task`` i tasques programades i una sessió SMTP/STARTTLS real
contra un servidor local. No envien correu a Internet.

En producció, utilitza PostgreSQL: l’estat persistent abans de la xarxa necessita
transaccions independents de la tasca del worker. SQLite queda per a desenvolupament i proves
del processador invocat fora d’una transacció que bloquegi escriptures.
