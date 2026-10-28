"""Vigia de restrições: link da bio e bloqueio de mensagem, conta por conta.

Roda de 2 em 2 horas pelo Agendador do Windows. Só lê: nada é alterado no banco
nem nas contas. Grava o relatório em vigia_restricoes/ e o estado da rodada
anterior para marcar o que é novo e o que voltou ao normal.

    python vigia_restricoes.py            # janela de 2 h
    python vigia_restricoes.py --horas 24
"""
import argparse
import base64
import json
from datetime import datetime
from pathlib import Path

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

RAIZ = Path(__file__).resolve().parent
PAP = RAIZ / "PostAutoPilot"
SAIDA = RAIZ / "vigia_restricoes"
ESTADO = SAIDA / "estado.json"
PROJETO = "liqfrqyumcthqdpbqmws"  # banco desde 15/10/2026 (o bgfff… foi bloqueado por egress)
DONOS = {"00000000": "Luderson", "1030dc6c": "Eduardo"}
TESTE = {"ludersonlsa2", "luderson_soares", "vinceree_co"}

# erro de DM que é restrição da conta; o resto (comentário antigo, usuário
# sumiu, fora da janela de 24 h) é rotina da Meta e não entra no alerta
BLOQUEIO_DM = ("temporarily blocked", "impedida tempora", "deemed abusive",
               "permissions error", "too many", "user access is restricted")


def env(caminho: Path) -> dict:
    out = {}
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        if "=" in linha and not linha.lstrip().startswith("#"):
            k, v = linha.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def sql(token: str, consulta: str) -> list:
    r = requests.post(f"https://api.supabase.com/v1/projects/{PROJETO}/database/query",
                      headers={"Authorization": "Bearer " + token},
                      json={"query": consulta}, timeout=60)
    r.raise_for_status()
    return r.json()


def checar_bio(chave: AESGCM, cifrado: str) -> tuple[str, str]:
    """('ok'|'sem_link'|'link_trocado'|'desconectada', detalhe)"""
    try:
        b = base64.b64decode(cifrado)
        tok = chave.decrypt(b[:12], b[12:], None).decode()
        j = requests.get("https://graph.instagram.com/v23.0/me",
                         params={"fields": "username,website", "access_token": tok},
                         timeout=30).json()
    except Exception as e:  # rede ou token ilegível
        return "desconectada", str(e)[:80]
    if "error" in j:
        return "desconectada", str(j["error"].get("message", ""))[:80]
    site = j.get("website") or ""
    if not site:
        return "sem_link", ""
    if "firetvplay" not in site:
        return "link_trocado", site[:80]
    return "ok", site.split("?")[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horas", type=int, default=2)
    a = ap.parse_args()

    cfg = env(PAP / ".env")
    token = env(PAP / "tokens_supabase_proton.env")["SUPABASE_ACCESS_TOKEN_PROTON"]
    chave = AESGCM(base64.b64decode(cfg["TOKEN_ENC_KEY"]))
    janela = f"now() - interval '{a.horas} hours'"

    contas = sql(token, "select id, username, ativo, token_cifrado, espaco_id from contas order by username")
    dms = {r["conta_id"]: r for r in sql(token, f"""
        select conta_id, count(*) enviadas,
               count(*) filter (where erro is not null) erros,
               array_agg(distinct left(erro, 120)) filter (where erro is not null) textos
        from mensagens_fluxo where direcao = 'saida' and em > {janela} group by 1""")}
    coments = {r["conta_id"]: r for r in sql(token, f"""
        select conta_id, count(*) total, count(*) filter (where acao = 'dm') viraram_dm,
               array_agg(distinct left(erro, 120)) filter (where erro is not null) textos
        from comentarios where em > {janela} group by 1""")}

    linhas = []
    for c in contas:
        if c["username"] in TESTE:
            continue
        bio, detalhe = checar_bio(chave, c["token_cifrado"])
        d = dms.get(c["id"], {})
        k = coments.get(c["id"], {})
        textos = (d.get("textos") or []) + (k.get("textos") or [])
        bloqueio = sorted({t for t in textos if any(p in t.lower() for p in BLOQUEIO_DM)})
        parada = (k.get("total", 0) >= 10 and not d.get("enviadas")
                  and bio != "desconectada")
        if bio == "desconectada":
            dm = "desconectada"
        elif bloqueio:
            dm = "bloqueada"
        elif parada:
            dm = "automacao_parada"
        else:
            dm = "ok"
        linhas.append({
            "conta": c["username"], "dono": DONOS.get(c["espaco_id"][:8], "?"),
            "ligada": c["ativo"], "bio": bio, "bio_detalhe": detalhe, "dm": dm,
            "dm_detalhe": bloqueio[0] if bloqueio else "",
            "comentarios": k.get("total", 0), "dms_enviadas": d.get("enviadas", 0),
            "dms_com_erro": d.get("erros", 0),
        })

    anterior = json.loads(ESTADO.read_text(encoding="utf-8")) if ESTADO.exists() else {}
    novos, voltaram = [], []
    for l in linhas:
        antes = anterior.get(l["conta"], {})
        for campo in ("bio", "dm"):
            if antes and antes.get(campo) != l[campo]:
                (voltaram if l[campo] == "ok" else novos).append(f"{l['conta']}: {campo} {antes.get(campo)} -> {l[campo]}")

    agora = datetime.now()
    rel = {"em": agora.isoformat(timespec="seconds"), "janela_horas": a.horas,
           "novos": novos, "voltaram": voltaram, "contas": linhas}
    SAIDA.mkdir(exist_ok=True)
    (SAIDA / f"relatorio_{agora:%Y-%m-%d_%H%M}.json").write_text(
        json.dumps(rel, ensure_ascii=False, indent=1), encoding="utf-8")
    ESTADO.write_text(json.dumps({l["conta"]: {"bio": l["bio"], "dm": l["dm"]} for l in linhas},
                                 ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"Vigia {agora:%d/%m %H:%M} · janela {a.horas} h")
    for titulo, filtro in (("SEM LINK / LINK TROCADO", lambda l: l["bio"] in ("sem_link", "link_trocado")),
                           ("DM BLOQUEADA", lambda l: l["dm"] == "bloqueada"),
                           ("AUTOMACAO PARADA", lambda l: l["dm"] == "automacao_parada"),
                           ("DESCONECTADA", lambda l: l["bio"] == "desconectada")):
        sel = [l for l in linhas if filtro(l)]
        print(f"\n{titulo} ({len(sel)})")
        for l in sel:
            extra = l["dm_detalhe"] or l["bio_detalhe"]
            print(f"  {l['conta']} ({l['dono']}) {extra[:70]}")
    if novos:
        print("\nNOVO desde a última rodada:\n  " + "\n  ".join(novos))
    if voltaram:
        print("\nVOLTOU AO NORMAL:\n  " + "\n  ".join(voltaram))


if __name__ == "__main__":
    main()
