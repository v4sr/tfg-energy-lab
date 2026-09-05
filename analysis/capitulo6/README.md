# Análisis reproducible del capítulo 6

## Dataset

El análisis utiliza exclusivamente el paquete
`datasets/hpc_tfg_main_campaign-20260723.tar.gz`. Contiene 61 ejecuciones de
`hpc_tfg_main_campaign` realizadas el 23 de julio de 2026:

- 1 ejecución Idle;
- 30 ejecuciones de `memory-stream-hpc`;
- 30 ejecuciones de `compute-dgemm-hpc`.

Las cargas activas combinan los perfiles `p1`, `p2`, `p4`, `p6`, `p8` y
`p12` con cinco repeticiones. Todas las ejecuciones se realizaron en
`compute-0-4`, asociado al dispositivo Vampire `hpm4`. No se incorporan
resultados de días anteriores.

## Filtros y controles

Una ejecución solo se acepta cuando:

1. su `run_id` comienza por `20260723_`;
2. SLURM informa `COMPLETED` y código `0:0`;
3. el nodo es `compute-0-4` y el dispositivo es `hpm4`;
4. existen el JSON, ambos CSV y el payload Prometheus;
5. la captura Vampire está completada y cubre la ventana del benchmark;
6. los agregados RAPL y Vampire coinciden con el recálculo desde los CSV;
7. el payload contiene las métricas internas y externas;
8. cada carga activa produce una salida por proceso.

Las 61 ejecuciones superan estos controles. No se descarta ninguna ejecución
del paquete del 23/07. Los resultados de campañas anteriores se excluyen por
fecha y por no pertenecer al conjunto final validado.

## Fórmulas

Para RAPL, cada incremento se obtiene de la diferencia de los contadores
`energy_uj` de los dominios raíz `package`, con tratamiento de desbordamiento,
y se convierte de microjulios a julios:

`delta_energy_j = sum(delta_energy_uj_package) / 1e6`

La potencia de muestra es `delta_energy_j / interval_s`; la energía total es
el último acumulado, la potencia media almacenada es la media aritmética de
las muestras y la máxima es su máximo.

Para Vampire se interpola la potencia en los límites del benchmark y se aplica
integración trapezoidal:

`energy_j = sum((p_i + p_(i+1)) / 2 * (t_(i+1) - t_i))`

La potencia media externa es `energy_j / duration_s`. La cobertura RAPL es
`100 * rapl_energy_j / vampire_energy_j`. La diferencia es
`vampire_energy_j - rapl_energy_j`.

El *speedup* emplea el rendimiento medio del perfil `p1` como referencia. La
eficiencia paralela es `speedup / cores`; la eficiencia energética relativa es
el cociente entre el aumento relativo del rendimiento y el aumento relativo
de la energía respecto a `p1`.

## Regeneración

Desde la raíz del repositorio:

```bash
python3 scripts/analyze_chapter6_20260723.py
```

El comando vuelve a crear los CSV de análisis, las tablas LaTeX en
`memoria/analysis/capitulo6/tablas/` y las figuras en
`memoria/imagenes/capitulo6/`. No modifica el archivo de datos brutos.

## Alcance

La concordancia entre CSV, agregados y payload valida el procesamiento y la
publicación local. La asociación física corregida permite comparar ambas
fuentes en la campaña final. Esta comprobación no equivale a una calibración
metrológica del dispositivo ni permite atribuir la diferencia entre Vampire y
RAPL a componentes concretos.
