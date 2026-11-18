Ejecuta el ciclo diario del trader (después del cierre del mercado):

0. Revisa `estado_cartera` y `decisiones_riesgo` para saber qué pasó ayer.
1. Usa el subagente `planificador` para abrir el ciclo de hoy y correr una ronda.
2. Usa el subagente `analista` hasta vaciar las intenciones "analyze".
3. Usa el subagente `planificador` para otra ronda (convierte hallazgos en intenciones de propuesta).
4. Usa el subagente `estratega` hasta vaciar las intenciones "propose".
5. Termina con `estado_ciclo` y resume en 5 líneas: hechos, hallazgos, propuestas pendientes y bloqueos.

Las propuestas las procesa `trader-ejecutor` (el humano o el Programador de tareas lo corre después); no lo ejecutes tú.

Mantén el uso bajo: no repitas análisis de instrumentos ya cubiertos hoy.
