# Dashboards actuales integrados con Vampire

Estos JSON proceden de las exportaciones actuales facilitadas por el usuario y
se han adaptado para incluir Vampire como una serie adicional en los paneles
existentes siempre que tecnicamente tenia sentido.

Archivos:

- `tfg-hpc-summary-v2-vampire.json`
- `tfg-hpc-scaling-v2-vampire.json`
- `tfg-hpc-live-vampire-ready.json`

## Integracion realizada

`TFG - HPC Run Summary`:

- Se ha redisenado para explicar visualmente que RAPL y Vampire tienen
  distinto alcance de medida.
- Energia, potencia media, potencia maxima, diferencia `Vampire - RAPL` y
  cobertura RAPL se muestran con colores semanticos estables.
- Las graficas temporales basadas en gauges agregados de Pushgateway se han
  eliminado: esos datos representan resultados finales de trabajos batch, no
  perfiles instantaneos de potencia.
- Las comparativas por cores usan medias por benchmark y numero de cores.
- La fila de resumen funciona mejor seleccionando un unico `run_id`.
- El detalle de paneles y consultas queda documentado en
  `tfg-hpc-summary-v2-vampire-redesign.md`.

`TFG - HPC Scaling & Efficiency`:

- Potencia por cores: RAPL + Vampire.
- Energia por cores: RAPL + Vampire.
- Eficiencia por cores: RAPL Ops/J + Vampire Ops/J.
- Ranking de eficiencia: RAPL + Vampire.
- Duracion por cores: duracion benchmark + duracion de captura Vampire.

`TFG - HPC Live Monitor`:

- Se conserva como RAPL live. No se añade una curva live Vampire desde
  Pushgateway porque no existe una serie temporal externa completa en
  Prometheus; esa curva debe venir de InfluxDB.

## Seguridad de importacion

Los dashboards se han guardado con nombres nuevos (`metadata.name` con sufijo
`vampire`) para no sobrescribir automaticamente los dashboards actuales del VPS.
Si quieres reemplazar los dashboards existentes, hazlo explicitamente desde
Grafana o ajustando el provisioning del VPS.
