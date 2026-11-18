Sesión semanal de mejora de estrategias (sin dinero, solo simulación):

1. Llama a `estrategias_disponibles` y a `credito_por_agente` para ver qué ha funcionado en paper.
2. Propón como máximo 5 variantes de parámetros con una hipótesis clara cada una (por qué deberían funcionar mejor).
3. Pruébalas con `lanzar_backtest`. Recuerda: cada prueba hace más exigente el Sharpe deflactado, así que no pruebes a ciegas.
4. Resume en una tabla: variante, CAGR, Sharpe, caída máxima, operaciones, exposición media, frente a comprar y mantener.
5. Recomienda como mucho una variante para que el humano la valide con
   `trader-backtest --estrategia <nombre> --modo walk-forward`. Nunca la des por buena solo con este resultado.
