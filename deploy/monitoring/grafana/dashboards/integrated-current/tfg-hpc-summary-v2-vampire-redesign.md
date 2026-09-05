# Rediseño: TFG - HPC Run Summary (RAPL + Vampire)

Este dashboard compara resultados agregados de trabajos batch publicados en Prometheus. No muestra perfiles temporales instantáneos del consumo durante el benchmark.

## Convención visual

- RAPL / medida interna: verde.
- Vampire / medida externa: amarillo.
- Diferencia `Vampire - RAPL`: azul.
- Captura fallida: rojo.
- Dato no disponible: gris.

## Paneles eliminados

| Panel anterior | Motivo |
| --- | --- |
| `Average Power Across Time (RAPL / Vampire)` | Mostraba gauges agregados repetidos por scrape, no una serie temporal física. |
| `Energy Across Time (RAPL / Vampire)` | Mostraba resultados finales como líneas temporales horizontales. |

## Paneles principales

| Panel | Métrica o consulta base | Unidad | Interpretación |
| --- | --- | --- | --- |
| ¿Qué se está comparando? | Texto | - | Explica el alcance distinto de RAPL y Vampire. |
| Energía consumida: RAPL frente a Vampire | `tfg_internal_energy_joules`, `tfg_external_energy_joules` | J | Energía final de la ejecución seleccionada en ambas fuentes. |
| Potencia media: RAPL frente a Vampire | `tfg_internal_average_power_watts`, `tfg_external_average_power_watts` | W | Potencia media agregada para RAPL y Vampire. |
| Validez de la captura seleccionada | `tfg_run_status`, `tfg_internal_rapl_samples > 0`, `tfg_external_capture_success` | 0/1 | Indica si el job, RAPL y Vampire son válidos para la selección. |
| Potencia máxima: RAPL frente a Vampire | `tfg_internal_peak_power_watts`, `tfg_external_peak_power_watts` | W | Máximo observado por cada fuente durante su ventana procesada. |
| Duración: benchmark y ventana Vampire | `tfg_experiment_duration_seconds`, `tfg_external_duration_seconds` | s | Separa duración del benchmark y duración externa capturada. |
| Diferencia de alcance: Vampire - RAPL | `tfg_external_energy_joules - on(run_id) tfg_internal_energy_joules` | J | Energía no cubierta por RAPL, sin atribuirla a un componente concreto. |
| Porcentaje de energía externa cubierto por RAPL | `100 * RAPL / Vampire` | % | Fracción de la energía externa que cubre la medida RAPL. |
| Energía interna y externa por número de cores | `avg by (benchmark, cores)` de energía RAPL y Vampire | J | Media de repeticiones por benchmark y cores. |
| Potencia media interna y externa por número de cores | `avg by (benchmark, cores)` de potencia media | W | Escalado de potencia media por benchmark y cores. |
| Energía no cubierta por RAPL según número de cores | `avg by (benchmark, cores) (Vampire - RAPL)` | J | Diferencia media de alcance por benchmark y cores. |
| Cobertura RAPL por número de cores | `avg by (benchmark, cores) (100 * RAPL / Vampire)` | % | Cobertura media de RAPL respecto a Vampire. |
| Duración del benchmark por número de cores | `avg by (benchmark, cores) tfg_experiment_duration_seconds` | s | Escalabilidad temporal del benchmark, sin ventana Vampire. |
| Energía interna RAPL según número de cores | `avg by (benchmark, cores) tfg_internal_energy_joules` | J | Energía interna media por cores. |
| Energía externa Vampire según número de cores | `avg by (benchmark, cores) tfg_external_energy_joules` | J | Energía externa media por cores. |
| Rendimiento por número de cores | `avg by (benchmark, cores) tfg_run_ops_per_second` | ops/s | Solo aplicable a benchmarks con operaciones publicadas. |
| Eficiencia energética por número de cores | `tfg_run_ops_per_joule` y `tfg_run_ops_total / tfg_external_energy_joules` | ops/J | Eficiencia separada para energía RAPL y Vampire. |
| Estado general de la medición | `min` de estado de job, muestras RAPL y éxito Vampire | 0/1 | Resume si todas las series filtradas son válidas. |
| Número de muestras: RAPL y Vampire | `tfg_internal_rapl_samples`, `tfg_external_samples` | muestras | Control básico de calidad de muestreo. |
| Desajuste temporal: Vampire - benchmark | `tfg_external_duration_seconds - on(run_id) tfg_experiment_duration_seconds` | s | Diferencia entre ventana externa y duración del benchmark. |
| Capturas externas fallidas | `tfg_external_capture_success == 0` | 0/1 | Lista ejecuciones con Vampire fallido. |
| Resultados por ejecución | Consultas instantáneas de energía, potencia, duración, operaciones y estado | varias | Tabla para ordenar e inspeccionar ejecuciones individuales. |
| Última ejecución recibida | `1000 * max(tfg_run_completed_unixtime)` | fecha | Último resultado agregado visible en Prometheus. |
| Métricas publicadas por ejecución | `count by (__name__)` | series | Inventario técnico de métricas TFG presentes. |

## Paneles que requieren una única ejecución

La fila `Resumen de la ejecución seleccionada` es más clara cuando `Ejecución` (`run_id`) contiene un único valor. Si se seleccionan varias ejecuciones, Grafana mostrará una serie por ejecución.

## Paneles que agregan repeticiones

Las secciones `Comparación RAPL frente a Vampire` y `Escalabilidad y eficiencia energética` usan medias con `avg by (benchmark, cores)`. Si se filtra una única repetición, la media coincide con esa ejecución.

## Métricas que faltan para mejorar el dashboard

- Estado explícito de RAPL, independiente del número de muestras.
- Error textual de Vampire consultable desde una fuente externa a Prometheus, porque no debe publicarse como etiqueta.
- Serie temporal real de potencia Vampire desde InfluxDB para construir un perfil temporal sin simularlo con Pushgateway.
