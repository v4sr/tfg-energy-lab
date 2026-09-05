# Frontal HTTPS

Esta carpeta contiene una versión saneada del frontal HAProxy utilizado por el
TFG. Solo conserva las rutas de `tfg-energy-lab.vasr.es`; se han eliminado los
servicios ajenos al proyecto.

El contenedor `certbot-web` sirve el desafío HTTP-01 bajo
`/.well-known/acme-challenge/`. La solicitud y renovación de certificados con
Certbot se realiza fuera de este Compose. HAProxy espera ficheros PEM aptos para
su directiva `crt` en el directorio indicado por `HAPROXY_CERTS_DIR`.

No se versionan certificados, claves privadas, cuentas ACME ni el contenido del
directorio de desafíos.
