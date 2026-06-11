"""
Renderiza um mapa de calor da cobertura de dados (máscara M) do dataset STGNN.

Lê o .npz gerado por preprocessar.py (X, M, timestamps, estacoes, features) e
mostra, por estação e por mês, a fração de horas observadas. Um painel por
variável (nível, chuva, vazão); estações ordenadas pela cobertura total.

É a figura que comunica a estrutura/esparsidade do dataset: onde há dado (claro)
e onde a STGNN terá de imputar/prever (escuro).

Rodar com: uv run python render_mask.py
"""

import argparse

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def agregar_mensal(M, timestamps):
    """Agrega a máscara [T x N x F] para [meses x N x F] = fração observada por mês."""
    meses = np.array([t[:7] for t in timestamps])           # ISO -> "YYYY-MM"
    rotulos = sorted(set(meses.tolist()))
    idx = {m: i for i, m in enumerate(rotulos)}
    col = np.array([idx[m] for m in meses])                 # mês de cada hora
    nmes, (_, N, F) = len(rotulos), M.shape

    soma = np.zeros((nmes, N, F), dtype=np.float64)
    np.add.at(soma, col, M)                                 # acumula M por mês
    cont = np.bincount(col, minlength=nmes).astype(np.float64)
    return soma / cont[:, None, None], rotulos             # fração observada


def main():
    ap = argparse.ArgumentParser(description="Mapa de calor da cobertura (máscara M).")
    ap.add_argument("--npz", default="dataset_historico.npz")
    ap.add_argument("--saida", default="cobertura_mascara.png")
    args = ap.parse_args()

    d = np.load(args.npz, allow_pickle=False)
    M = d["M"]
    timestamps = d["timestamps"]
    features = [str(f) for f in d["features"]]
    T, N, F = M.shape

    cobertura, rotulos = agregar_mensal(M, timestamps)

    # ordena estações pela cobertura total (mais densas no topo)
    ordem = np.argsort(M.mean(axis=(0, 2)))[::-1]

    # ticks de tempo: início de cada ano presente
    anos = sorted({r[:4] for r in rotulos})
    xt = [(rotulos.index(f"{a}-01"), a) for a in anos if f"{a}-01" in rotulos]

    fig, axes = plt.subplots(1, F, figsize=(5 * F, 9), sharey=True)
    im = None
    for fi, ax in enumerate(axes):
        im = ax.imshow(cobertura[:, ordem, fi].T, aspect="auto", cmap="viridis",
                       vmin=0, vmax=1, interpolation="nearest")
        cov = M[:, :, fi].mean() * 100
        nos = int((M[:, :, fi].sum(axis=0) > 0).sum())
        ax.set_title(f"{features[fi]}\n{cov:.0f}% obs. | {nos}/{N} estações")
        ax.set_xticks([p for p, _ in xt])
        ax.set_xticklabels([a for _, a in xt])
        ax.set_xlabel("ano")
        if fi == 0:
            ax.set_ylabel(f"estações (N={N}, ordenadas por cobertura)")

    cbar = fig.colorbar(im, ax=axes, shrink=0.7, pad=0.02)
    cbar.set_label("fração de horas observadas no mês  (1 = completo, 0 = ausente)")

    obs = M.mean() * 100
    fig.suptitle(f"Cobertura de dados — dataset STGNN  "
                 f"[T={T} h x N={N} estações x F={F} variáveis]  "
                 f"|  {obs:.0f}% observado, {100-obs:.0f}% ausente",
                 fontsize=12)
    fig.savefig(args.saida, dpi=150, bbox_inches="tight")
    print(f"Salvo: {args.saida}  ({N} estações x {len(rotulos)} meses x {F} variáveis)")


if __name__ == "__main__":
    main()
