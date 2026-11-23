"""Gera `dados_sinteticos.csv` (N=500) — dataset epidemiológico TOTALMENTE SINTÉTICO.

Cenário fictício: idosos atendidos em uma rede municipal, exposição semanal a PM2.5 e internação
respiratória. Inclui missings propositais, uma categoria rara (<5) para testar supressão de células,
e colunas identificadoras falsas (cns, data_nascimento) para testar a detecção de PII.

Uso: python gerar_dataset_sintetico.py  (determinístico, semente fixa)
"""

from __future__ import annotations

import csv
import math
import random
from datetime import date, timedelta
from pathlib import Path

N = 500
rng = random.Random(20240501)
OUT = Path(__file__).with_name("dados_sinteticos.csv")


def maybe(value, p_missing):
    return "" if rng.random() < p_missing else value


rows = []
start = date(2018, 1, 1)
for i in range(1, N + 1):
    idade = int(min(98, max(60, rng.gauss(72, 8))))
    sexo = rng.choice(["F", "M"])
    distrito = "DS-VIII" if i in (17, 230, 401) else rng.choice(["DS-I", "DS-II", "DS-III", "DS-IV", "DS-V", "DS-VI"])
    notif = start + timedelta(days=rng.randint(0, 4 * 365))
    nasc = notif - timedelta(days=idade * 365 + rng.randint(0, 364))
    inverno = notif.month in (5, 6, 7, 8)
    pm25 = max(3.0, rng.gauss(22 if inverno else 15, 6))
    temp = rng.gauss(24 if inverno else 28, 2)
    tabagismo = rng.choices(["nunca", "ex", "atual"], [0.55, 0.30, 0.15])[0]
    escolaridade = rng.choices(["fundamental", "medio", "superior"], [0.55, 0.32, 0.13])[0]
    raca = rng.choices(["parda", "branca", "preta", "amarela", "indigena"], [0.52, 0.30, 0.15, 0.02, 0.01])[0]
    lin = -4.2 + 0.045 * (pm25 - 15) + 0.035 * (idade - 72) + (0.5 if tabagismo == "atual" else 0.2 if tabagismo == "ex" else 0) + (0.15 if sexo == "M" else 0)
    p = 1 / (1 + math.exp(-(lin + 2.6)))
    intern = 1 if rng.random() < p else 0
    dias = max(1, int(rng.expovariate(1 / 6))) if intern else 0
    cns = "7" + "".join(str(rng.randint(0, 9)) for _ in range(14))
    rows.append({
        "id_registro": i,
        "cns": cns,
        "data_nascimento": nasc.isoformat(),
        "data_notificacao": notif.isoformat(),
        "idade": idade,
        "sexo": sexo,
        "raca_cor": maybe(raca, 0.06),
        "escolaridade": maybe(escolaridade, 0.12),
        "distrito_sanitario": distrito,
        "pm25_media_7d": maybe(round(pm25, 2), 0.04),
        "temperatura_media_7d": round(temp, 2),
        "tabagismo": maybe(tabagismo, 0.08),
        "internacao_respiratoria": intern,
        "dias_internacao": dias,
    })

with open(OUT, "w", newline="", encoding="utf-8") as fh:
    writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
print(f"{OUT.name}: {len(rows)} linhas")
