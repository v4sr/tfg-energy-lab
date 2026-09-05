# Sistema de medida energética para HPMoon

Este repositorio contiene el sistema desarrollado para el TFG **Sistema de
medida del consumo energético de las CPU de un clúster de computadores**. La
versión pública se limita a los componentes utilizados en la campaña final del
23 de julio de 2026 y al material necesario para reproducir su análisis.

## Contenido

- `campaigns/hpc-tfg-main-campaign.json`: configuración de la campaña final.
- `benchmarks/c/memory-stream-hpc/`: carga orientada a memoria.
- `benchmarks/c/compute-dgemm-hpc/`: carga orientada a cálculo.
- `run_matrix.py` y `run_experiment.py`: automatización de campañas y
  ejecuciones individuales.
- `rapl_sampler.py`: adquisición periódica de los dominios RAPL.
- `vampire_integration.py`: coordinación de la fuente de medida externa.
- `monitoring.py`: publicación de métricas agregadas en Pushgateway.
- `slurm/templates/`: plantilla de ejecución mediante SLURM.
- `analysis/capitulo6/`: dataset final, controles, tablas y resultados
  derivados.
- `scripts/analyze_chapter6_20260723.py`: reproducción del análisis conjunto.
- `deploy/monitoring/`: despliegue saneado de HAProxy, Pushgateway, Prometheus
  y Grafana.
- `memoria/`: fuentes LaTeX del TFG.

Los resultados de campañas preliminares, registros de ejecución, volúmenes de
servicios, credenciales, certificados, binarios compilados y paquetes de
diagnóstico no forman parte de la versión pública.

## Preparación

Se necesita Python 3 y un compilador C. Las dependencias utilizadas por los
scripts de análisis se instalan mediante:

```bash
python3 -m venv env
source env/bin/activate
pip install -r requirements.txt
make
```

La ejecución real requiere acceso a HPMoon, SLURM y a la interfaz `powercap`
de los nodos de cómputo.

## Medida externa

El cliente de Vampire es una dependencia externa facilitada por la Universidad
de Granada y no forma parte del software desarrollado ni se distribuye en este
repositorio. Su ubicación se indica antes de lanzar la campaña:

```bash
export VAMPIRE_CLIENT_PATH=/ruta/al/cliente/vampire.py
```

La configuración final asocia `compute-0-4` con el dispositivo `hpm4`. El
módulo `vampire_integration.py`, desarrollado en este proyecto, coordina las
operaciones de inicio, parada y descarga del cliente externo, valida la
cobertura temporal y conserva el estado de cada captura.

## Ejecución de la campaña

La URL de Pushgateway se proporciona mediante una variable de entorno:

```bash
export PUSHGATEWAY_URL=https://tfg-energy-lab.vasr.es/pushgateway
python3 run_matrix.py --config campaigns/hpc-tfg-main-campaign.json
```

Las salidas de ejecución se escriben bajo `results/`, que está excluido del
control de versiones.

## Reproducción del capítulo 6

El paquete de datos validado contiene 61 ejecuciones realizadas el 23 de julio
de 2026: una ejecución Idle y cinco repeticiones de STREAM y DGEMM para los
perfiles de 1, 2, 4, 6, 8 y 12 procesos.

```bash
python3 scripts/analyze_chapter6_20260723.py
```

El procedimiento, los filtros y las fórmulas se documentan en
[`analysis/capitulo6/README.md`](analysis/capitulo6/README.md).

## Pruebas

Las pruebas locales no contactan con SLURM, Vampire ni los servicios remotos:

```bash
python3 -m unittest discover -s tests
```

## Observabilidad

La configuración reproducible del VPS se encuentra en `deploy/monitoring/`.
Los datos persistentes, las claves TLS y las credenciales se proporcionan de
forma externa y permanecen fuera de Git.
