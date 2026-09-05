# Despliegue de observabilidad del TFG

Esta carpeta conserva una versión reproducible y saneada del despliegue
utilizado en el VPS Ubuntu 24.04. Solo contiene configuración: quedan fuera del
repositorio los datos de Prometheus, la base de datos y los complementos de
Grafana, certificados, claves, cuentas ACME, credenciales y ficheros `.env`.

## Arquitectura

```text
HPMoon
  | HTTPS: /pushgateway
  v
HAProxy :443
  |-- /pushgateway/ --> pushgateway:9091
  |-- /prometheus/  --> prometheus:9090
  `-- /grafana/     --> grafana:3000

prometheus:9090 -- scrape interno --> pushgateway:9091
grafana:3000 ---- datasource interno --> prometheus:9090/prometheus
```

HAProxy es el único servicio que publica puertos del host. Los tres servicios
de observabilidad se anuncian mediante `expose` y se resuelven por el DNS de la
red Docker externa `front-lb`. Prometheus y Grafana comparten además la red
privada `monitoring`.

HPMoon publica los agregados en:

```text
https://tfg-energy-lab.vasr.es/pushgateway
```

La ruta pública ofrece una dirección estable y evita depender de la IP temporal
asignada al equipo de desarrollo por la VPN universitaria. También permite
consultar campañas largas aunque dicho equipo no permanezca conectado.

## Contenido versionado

- `docker-compose.yml`: Pushgateway, Prometheus y Grafana.
- `prometheus/prometheus.yml`: *scrape* interno de Pushgateway cada 2 s.
- `grafana/provisioning/`: fuente Prometheus y carga de dashboards.
- `grafana/dashboards/`: paneles exportados sin credenciales.
- `haproxy/`: frontal HTTPS limitado al dominio y rutas del TFG.
- `examples/env.example`: variables sin secretos.

El antiguo proxy Nginx incluido en una copia del VPS no forma parte de este
despliegue: el frontal vigente es HAProxy. Tampoco se conservan benchmarks,
resultados ni scripts antiguos incluidos accidentalmente junto a los volúmenes.

## Preparación

Crear la red compartida una sola vez:

```bash
docker network create front-lb
```

Crear un `.env` local a partir del ejemplo y revisar las rutas absolutas:

```bash
cp examples/env.example .env
```

Los directorios indicados por `MONITORING_DATA_DIR` son *bind mounts*. Deben
existir y tener permisos compatibles con las imágenes oficiales. Su contenido
es estado de ejecución y no debe incorporarse a Git.

## Arranque

Frontal:

```bash
docker compose --env-file .env -f haproxy/docker-compose.yml up -d
```

Observabilidad:

```bash
docker compose --env-file .env -f docker-compose.yml up -d
```

## Flujo de datos

1. `run_experiment.py` guarda primero CSV y JSON en HPMoon.
2. El cliente realiza un `PUT` HTTPS contra `/pushgateway`.
3. HAProxy termina TLS, elimina el prefijo y reenvía a `pushgateway:9091`.
4. Prometheus consulta `pushgateway:9091` por la red Docker interna.
5. Grafana consulta `http://prometheus:9090/prometheus` mediante su *datasource*.

Pushgateway solo contiene agregados por ejecución. Las muestras brutas y sus
timestamps permanecen en los CSV de campaña.

## Subrutas

Prometheus se inicia con `--web.external-url` y
`--web.route-prefix=/prometheus`. Grafana utiliza `GF_SERVER_ROOT_URL` y
`GF_SERVER_SERVE_FROM_SUB_PATH=true`. HAProxy normaliza las rutas sin barra
final y elimina `/pushgateway` antes de alcanzar la API del servicio.

## TLS

Certbot obtiene y renueva certificados emitidos por Let's Encrypt. El
contenedor `certbot-web` sirve el desafío HTTP-01 y HAProxy carga el material
PEM desde `HAPROXY_CERTS_DIR`. La automatización concreta de Certbot se mantiene
fuera del repositorio para no versionar claves privadas ni información ACME.

## Comprobaciones

```bash
curl -fsS https://tfg-energy-lab.vasr.es/pushgateway/-/healthy
curl -fsS https://tfg-energy-lab.vasr.es/prometheus/-/ready
curl -fsS https://tfg-energy-lab.vasr.es/grafana/api/health
```

En Prometheus, el objetivo `pushgateway:9091` debe aparecer con estado `UP`.
Una consulta mínima es:

```promql
{__name__=~"tfg_.*"}
```

## Seguridad y mantenimiento

- No copiar al repositorio `.env`, certificados, claves ni volúmenes.
- Mantener accesible desde Internet únicamente el frontal HTTPS.
- Limpiar selectivamente los grupos de Pushgateway que ya no sean necesarios.
- Realizar copias de seguridad solo de configuración y dashboards; la fuente
  científica continúa siendo el paquete de resultados conservado en HPMoon.
