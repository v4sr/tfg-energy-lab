# PromQL usado por el dashboard RAPL/Vampire

Los payloads actuales contienen labels:

```text
campaign, benchmark, language, profile, cores, threads, rep, node,
vampire_device, run_id, status
```

## Filtros base

```promql
tfg_external_energy_joules{
  campaign=~"$campaign",
  benchmark=~"$benchmark",
  profile=~"$profile",
  cores=~"$cores",
  threads=~"$threads",
  node=~"$node",
  run_id=~"$run_id"
}
```

`vampire_device` se reserva para la tabla de calidad. No se usa como filtro en
las magnitudes científicas ni como clave de unión.

## Estado de captura Vampire

```promql
tfg_external_capture_success{
  campaign=~"$campaign",
  benchmark=~"$benchmark",
  run_id=~"$run_id"
}
```

Fallos:

```promql
tfg_external_capture_success{
  campaign=~"$campaign",
  benchmark=~"$benchmark",
  run_id=~"$run_id"
} == 0
```

Numero de ejecuciones correctas:

```promql
sum(tfg_external_capture_success{
  campaign=~"$campaign",
  benchmark=~"$benchmark",
  run_id=~"$run_id"
})
```

Numero de ejecuciones fallidas:

```promql
sum(1 - tfg_external_capture_success{
  campaign=~"$campaign",
  benchmark=~"$benchmark",
  run_id=~"$run_id"
})
```

Ultima ejecucion recibida:

```promql
max(tfg_run_completed_unixtime{
  campaign=~"$campaign",
  benchmark=~"$benchmark",
  run_id=~"$run_id"
})
```

## Energia interna y externa

Energia interna RAPL:

```promql
tfg_internal_energy_joules{
  campaign=~"$campaign",
  benchmark=~"$benchmark",
  profile=~"$profile",
  run_id=~"$run_id"
}
```

Energia externa Vampire:

```promql
tfg_external_energy_joules{
  campaign=~"$campaign",
  benchmark=~"$benchmark",
  profile=~"$profile",
  run_id=~"$run_id"
}
```

Diferencia externa - interna:

```promql
tfg_external_energy_joules{
  campaign=~"$campaign",
  benchmark=~"$benchmark",
  profile=~"$profile",
  run_id=~"$run_id"
}
-
on(campaign,benchmark,profile,cores,threads,rep,node,run_id)
tfg_internal_energy_joules{
  campaign=~"$campaign",
  benchmark=~"$benchmark",
  profile=~"$profile",
  run_id=~"$run_id"
}
```

Cociente interna / externa evitando division por cero:

```promql
tfg_internal_energy_joules{
  campaign=~"$campaign",
  benchmark=~"$benchmark",
  profile=~"$profile",
  run_id=~"$run_id"
}
/
on(campaign,benchmark,profile,cores,threads,rep,node,run_id)
(
  tfg_external_energy_joules{
    campaign=~"$campaign",
    benchmark=~"$benchmark",
    profile=~"$profile",
    run_id=~"$run_id"
  } > 0
)
```

## Potencia

```promql
tfg_internal_average_power_watts{campaign=~"$campaign", benchmark=~"$benchmark", run_id=~"$run_id"}
```

```promql
tfg_external_average_power_watts{campaign=~"$campaign", benchmark=~"$benchmark", run_id=~"$run_id"}
```

```promql
tfg_internal_peak_power_watts{campaign=~"$campaign", benchmark=~"$benchmark", run_id=~"$run_id"}
```

```promql
tfg_external_peak_power_watts{campaign=~"$campaign", benchmark=~"$benchmark", run_id=~"$run_id"}
```

## Escalabilidad y tiempo

Energia externa frente a cores:

```promql
tfg_external_energy_joules{
  campaign=~"$campaign",
  benchmark=~"$benchmark",
  cores=~"$cores"
}
```

Tiempo frente a cores:

```promql
tfg_experiment_duration_seconds{
  campaign=~"$campaign",
  benchmark=~"$benchmark",
  cores=~"$cores"
}
```

No se usa `group_left` ni `group_right`: ambas fuentes salen de la misma fila y
son uno-a-uno para la identidad común. Si esa cardinalidad cambia, debe
corregirse el modelo antes de modificar PromQL.
