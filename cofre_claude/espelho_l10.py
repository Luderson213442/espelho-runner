# -*- coding: utf-8 -*-
"""Método do Eduardo nas contas do Luderson (28/10/2026).

Fontes: reels do Eduardo sem cabeçalho com CTA queimada "ASSISTA NO LINK DA BIO"
(famílias l10_*_link_, liso_*, lote_*_link_). Cada post recebe o SEU arquivo:
remux -c copy com comment=<conta>:<shortcode> e capa própria (JPEG regravado).

Regras do plano:
  (a) cena que o Eduardo posta no dia ou em ±2 dias não entra nesse dia;
  (b) no máximo 2 contas do Luderson por cena no mesmo dia;
  (c) teto total por cena (TETO), cena nunca repetida na mesma conta (nem cenas
      que a conta já postou antes);
  grade do Eduardo (10/dia: a cada 105 min desde 06:00; 20/dia: a cada 50 min),
  conta k deslocada 2k min (mod 40).

    python cofre_claude/espelho_l10.py plano [por_dia] [teto]   # dry-run -> plano.json
    python cofre_claude/espelho_l10.py rodar                    # baixa, remuxa, sobe, agenda (retoma)
    python cofre_claude/espelho_l10.py status
    python cofre_claude/espelho_l10.py conferir
"""
import hashlib
import io
import json
import random
import re
import subprocess
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import boto3
import requests
from PIL import Image

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
import vigia_restricoes as vr  # noqa: E402

PASTA = RAIZ / "backlog" / "espelho_l10"
FONTES = PASTA / "fontes"
SAIDA = PASTA / "saida"
PLANO = PASTA / "plano.json"
LOG = PASTA / "espelho.log"

LUD = "00000000-0000-4000-8000-000000000001"
EDU = "1030dc6c-e67a-4c29-8ecc-955449188500"
PULAR = {"zedascenas_", "kanguruufilmes"}   # "User access is restricted" no Instagram
INICIO, FIM = date(2026, 10, 29), date(2026, 11, 7)
GRADE = {10: (6 * 60, 105), 20: (6 * 60, 50)}   # (primeiro minuto, passo)
SP = timezone(timedelta(hours=-3))
UA = {"User-Agent": "Mozilla/5.0"}
FAMILIA = {"l10": 0, "liso": 1, "lote": 2}   # preferência da fonte
RE_SC = re.compile(r"^(l10|liso|lote)_(preto|branco)_(?:link_)?(.+)$")
_lock = threading.Lock()


def log(msg: str) -> None:
    linha = f"{datetime.now():%H:%M:%S} {msg}"
    with _lock:
        print(linha, flush=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(linha + "\n")


def banco():
    tok = vr.env(vr.PAP / "tokens_supabase_proton.env")["SUPABASE_ACCESS_TOKEN_PROTON"]

    def sql(q):
        for t in range(5):
            r = requests.post(f"https://api.supabase.com/v1/projects/{vr.PROJETO}/database/query",
                              headers={"Authorization": "Bearer " + tok}, json={"query": q}, timeout=120)
            if r.ok:
                return r.json()
            if r.status_code < 500 and r.status_code != 429:
                raise RuntimeError(f"SQL {r.status_code}: {r.text[:300]}")
            import time
            time.sleep(3 * (t + 1))
        raise RuntimeError(f"SQL {r.status_code}: {r.text[:300]}")
    return sql


def lit(s) -> str:
    if s is None:
        return "null"
    s = str(s)
    tag = "$e$"
    while tag in s:
        tag = "$e" + str(random.randint(0, 9999)) + "$"
    return f"{tag}{s}{tag}"


def gravar(p: dict) -> None:
    with _lock:
        tmp = PLANO.with_suffix(".tmp")
        tmp.write_text(json.dumps(p, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(PLANO)


def nome_do_filme(leg: str, slug: str) -> str:
    m = re.search(r"Nome d[oa] \w+\s*:\s*(.+)", leg or "", re.I)
    return m.group(1).strip().lower() if m else ""


# ------------------------------------------------------------------ plano
def montar_pool(sql) -> dict:
    linhas = sql(f"""
        select distinct on (m.cena_id) m.cena_id, m.fundo, m.url_video, m.url_capa, m.duracao_s, m.largura,
               m.altura, m.codec, m.titulo, m.filme_slug, m.legenda_sugerida, c.username
          from midias m join contas c on c.id = m.conta_id
         where m.espaco_id = '{EDU}' and (m.cena_id like 'l10\\_%' or m.cena_id like 'liso\\_%'
               or m.cena_id like 'lote\\_%\\_link\\_%')
           and m.legenda_sugerida ilike '%link da bio%'
         order by m.cena_id, m.criado_em desc""")
    pool: dict = {}
    for r in linhas:
        fam, fundo, sc = RE_SC.match(r["cena_id"]).groups()
        leg = r["legenda_sugerida"]
        if f"(@{r['username']})" not in leg:
            continue
        x = pool.setdefault(sc, {"sc": sc, "fontes": {}, "filme": ""})
        atual = x["fontes"].get(fundo)
        if atual and FAMILIA[atual["fam"]] <= FAMILIA[fam]:
            continue
        x["fontes"][fundo] = {k: r[k] for k in ("cena_id", "fundo", "url_video", "url_capa", "duracao_s",
                                                "largura", "altura", "codec", "titulo", "filme_slug")}
        x["fontes"][fundo]["fam"] = fam
        x["fontes"][fundo]["legenda"] = leg.replace(f"(@{r['username']})", "(@{ARROBA})")
        x["filme"] = nome_do_filme(leg, r["filme_slug"]) or x["filme"] or r["filme_slug"]
    return pool


def dias_do_eduardo(sql) -> dict:
    out = defaultdict(set)
    for r in sql(f"""
        select distinct m.cena_id, (p.agendado_para at time zone 'America/Sao_Paulo')::date d
          from posts p join midias m on m.id = p.midia_id
         where m.espaco_id = '{EDU}' and p.apagado_em is null and p.status <> 'cancelado'
           and p.agendado_para >= '2026-10-25' and p.agendado_para < '2026-11-12'
           and (m.cena_id like 'l10\\_%' or m.cena_id like 'liso\\_%' or m.cena_id like 'lote\\_%')"""):
        mm = re.match(r"^(?:l10|liso|lote)_(?:preto|branco)_(?:link_|quero_)?(.+)$", r["cena_id"])
        if mm:
            out[mm.group(1)].add(date.fromisoformat(r["d"]))
    return out


def contas(sql) -> list:
    cs = sql(f"""select id, username, espaco_id from contas where espaco_id = '{LUD}' and ativo = true
                  and token_cifrado is not null and token_expira_em > now() order by criado_em, username""")
    return [c for c in cs if c["username"] not in PULAR]


def plano(por_dia: int = 10, teto: int = 15) -> None:
    sql = banco()
    pool = montar_pool(sql)
    edu = dias_do_eduardo(sql)
    cs = contas(sql)
    # cenas que cada conta já postou/agendou (qualquer formato)
    ja = defaultdict(set)
    scs = set(pool)
    for r in sql(f"""select distinct p.conta_id, m.cena_id from posts p join midias m on m.id = p.midia_id
                      join contas c on c.id = p.conta_id where c.espaco_id = '{LUD}' and p.apagado_em is null
                        and p.status <> 'cancelado'"""):
        cid = r["cena_id"]
        for sc in scs:
            if sc in cid:
                ja[r["conta_id"]].add(sc)
    total = Counter()
    rng = random.Random(2810)
    dias = [INICIO + timedelta(days=i) for i in range((FIM - INICIO).days + 1)]
    ini, passo = GRADE[por_dia]
    itens, faltou = [], Counter()
    usados = defaultdict(set)
    filmes_dia = defaultdict(set)
    for d in dias:
        nodia = Counter()
        livres = [sc for sc in pool if not any(abs((d - e).days) <= 2 for e in edu.get(sc, ()))]
        for s in range(por_dia):
            ordem = list(range(len(cs)))
            rng.shuffle(ordem)
            for k in ordem:
                c = cs[k]
                cand = [sc for sc in livres if nodia[sc] < 2 and total[sc] < teto
                        and sc not in usados[c["id"]] and sc not in ja[c["id"]]
                        and pool[sc]["filme"] not in filmes_dia[(c["id"], d)]]
                if not cand:
                    faltou[d.isoformat()] += 1
                    continue
                rng.shuffle(cand)
                sc = min(cand, key=lambda x: (total[x], nodia[x]))
                total[sc] += 1
                nodia[sc] += 1
                usados[c["id"]].add(sc)
                filmes_dia[(c["id"], d)].add(pool[sc]["filme"])
                fundo = ("preto", "branco")[(k + s) % 2]
                if fundo not in pool[sc]["fontes"]:
                    fundo = next(iter(pool[sc]["fontes"]))
                minuto = ini + s * passo + (2 * k) % 40
                quando = datetime(d.year, d.month, d.day, tzinfo=SP) + timedelta(minutes=minuto)
                itens.append({"conta": c["username"], "conta_id": c["id"], "espaco_id": c["espaco_id"],
                              "sc": sc, "fundo": fundo, "filme": pool[sc]["filme"],
                              "quando": quando.isoformat(), "estado": "planejado"})
    itens.sort(key=lambda it: it["quando"])
    PASTA.mkdir(parents=True, exist_ok=True)
    usados_sc = {it["sc"] for it in itens}
    gravar({"criado": datetime.now().isoformat(), "por_dia": por_dia, "teto": teto,
            "reels": {sc: pool[sc] for sc in usados_sc}, "itens": itens})
    porc = Counter(it["conta"] for it in itens)
    print(f"contas: {len(cs)} | pool: {len(pool)} cenas | posts: {len(itens)} | cenas usadas: {len(usados_sc)}")
    print("faltou por dia:", dict(faltou))
    print("posts por conta (min/max):", min(porc.values()), max(porc.values()))
    print("uso por cena: max", max(total.values()), "| distribuição", sorted(Counter(total.values()).items()))
    print("fundo:", Counter(it["fundo"] for it in itens), "| família:",
          Counter(pool[it["sc"]]["fontes"][it["fundo"]]["fam"] for it in itens))
    print("livres por dia:", {d.isoformat()[5:]: sum(1 for sc in pool if not any(abs((d - e).days) <= 2
                                                                         for e in edu.get(sc, ()))) for d in dias})


# ------------------------------------------------------------------ rodar
def r2():
    cfg = vr.env(vr.PAP / ".env")
    s3 = boto3.client("s3", endpoint_url=f"https://{cfg['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                      aws_access_key_id=cfg["R2_ACCESS_KEY_ID"], aws_secret_access_key=cfg["R2_SECRET_ACCESS_KEY"],
                      region_name="auto")
    return s3, cfg["R2_BUCKET"], cfg["R2_PUBLIC_BASE_URL"].rstrip("/")


_baixando: dict = {}


def baixar(url: str, destino: Path) -> Path:
    with _lock:
        ev = _baixando.get(str(destino))
        dono = ev is None
        if dono:
            ev = _baixando[str(destino)] = threading.Event()
    if not dono:
        ev.wait()
        return destino
    try:
        if not (destino.exists() and destino.stat().st_size > 50_000):
            destino.parent.mkdir(parents=True, exist_ok=True)
            tmp = destino.with_suffix(destino.suffix + ".part")
            with requests.get(url, headers=UA, stream=True, timeout=180) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for bloco in r.iter_content(1 << 20):
                        f.write(bloco)
            tmp.replace(destino)
    finally:
        ev.set()
    return destino


def sha(caminho: Path) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def um(p: dict, it: dict, sql, s3, bucket: str, pub: str) -> None:
    fte = p["reels"][it["sc"]]["fontes"][it["fundo"]]
    agora = datetime.now(timezone.utc)
    if datetime.fromisoformat(it["quando"]) < agora + timedelta(minutes=10):
        it["estado"] = "perdido"
        log(f"perdido {it['conta']} {it['quando']}")
        return
    try:
        ja = sql(f"""select p.id, p.midia_id, m.url_video, m.url_capa from posts p join midias m on m.id = p.midia_id
                      where p.conta_id = {lit(it['conta_id'])} and m.origem = {lit('espelho_l10/' + fte['cena_id'])}
                        and p.apagado_em is null""")
        if ja:
            it.update(estado="agendado", post_id=ja[0]["id"], midia_id=ja[0]["midia_id"],
                      url_video=ja[0]["url_video"], url_capa=ja[0]["url_capa"])
            return
        src = baixar(fte["url_video"], FONTES / f"{fte['cena_id']}.mp4")
        SAIDA.mkdir(parents=True, exist_ok=True)
        base = f"{it['conta']}__{it['sc']}"
        mp4 = SAIDA / f"{base}.mp4"
        tmp = SAIDA / f"{base}.tmp.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-map", "0", "-c", "copy",
                        "-map_metadata", "-1", "-metadata", f"comment={it['conta']}:{it['sc']}",
                        "-movflags", "+faststart", str(tmp)], check=True)
        tmp.replace(mp4)
        h = sha(mp4)
        tam = mp4.stat().st_size
        chave = f"reels/{h[:32]}.mp4"
        s3.upload_file(str(mp4), bucket, chave,
                       ExtraArgs={"ContentType": "video/mp4", "CacheControl": "public, max-age=31536000"})
        url_v = f"{pub}/{chave}"
        url_c = None
        if fte.get("url_capa"):
            capa = baixar(fte["url_capa"], FONTES / f"{fte['cena_id']}.jpg")
            buf = io.BytesIO()
            Image.open(capa).convert("RGB").save(buf, "JPEG", quality=90,
                                                 comment=f"{it['conta']}:{it['sc']}".encode())
            dados = buf.getvalue()
            ck = f"capas/{hashlib.sha256(dados).hexdigest()[:32]}.jpg"
            s3.put_object(Bucket=bucket, Key=ck, Body=dados, ContentType="image/jpeg",
                          CacheControl="public, max-age=31536000")
            url_c = f"{pub}/{ck}"
        mp4.unlink()
        legenda = fte["legenda"].replace("{ARROBA}", it["conta"])
        if len(legenda.strip()) < 20 or "@" + it["conta"] not in legenda:
            raise RuntimeError("legenda inválida")
        r = sql(f"""
            with m as (
              insert into midias (origem, filme_slug, cena_id, fundo, url_video, url_capa, duracao_s, largura,
                                  altura, codec, tamanho_bytes, sha256, titulo, headline, legenda_sugerida,
                                  espaco_id, tipo, conta_id)
              values ({lit('espelho_l10/' + fte['cena_id'])}, {lit(fte['filme_slug'])}, {lit(fte['cena_id'])},
                      {lit(fte['fundo'])}, {lit(url_v)}, {lit(url_c)}, {fte['duracao_s']}, {fte['largura'] or 1080},
                      {fte['altura'] or 1920}, {lit(fte['codec'])}, {tam}, {lit(h)}, {lit(fte['titulo'])},
                      {lit(fte['titulo'])}, {lit(legenda)}, {lit(it['espaco_id'])}, 'video', {lit(it['conta_id'])})
              returning id)
            insert into posts (conta_id, midia_id, agendado_para, legenda, status, formato, na_grade)
            select {lit(it['conta_id'])}, m.id, {lit(it['quando'])}, {lit(legenda)}, 'agendado', 'reels', true
              from m
            returning id, midia_id""")
        it.update(estado="agendado", post_id=r[0]["id"], midia_id=r[0]["midia_id"], url_video=url_v, url_capa=url_c)
        log(f"ok {it['conta']:18} {it['quando'][5:16]} {it['sc']} {it['fundo']}")
    except Exception as e:  # noqa: BLE001
        it["estado"] = "erro"
        it["erro"] = str(e)[:200]
        log(f"ERRO {it['conta']} {it['sc']}: {e}")


def limpar_fontes(p: dict) -> None:
    """Apaga fontes que nenhum item pendente usa mais (disco)."""
    pend = {p["reels"][it["sc"]]["fontes"][it["fundo"]]["cena_id"] for it in p["itens"]
            if it["estado"] not in ("agendado", "perdido")}
    for f in FONTES.glob("*.mp4"):
        if f.stem not in pend:
            try:
                f.unlink()
            except OSError:
                pass


def rodar(paralelo: int = 6) -> None:
    p = json.loads(PLANO.read_text(encoding="utf-8"))
    sql = banco()
    s3, bucket, pub = r2()
    pend = [it for it in p["itens"] if it["estado"] not in ("agendado", "perdido")]
    log(f"rodar: {len(pend)} pendentes")
    feitos = 0
    with ThreadPoolExecutor(paralelo) as ex:
        for i in range(0, len(pend), 60):
            list(ex.map(lambda it: um(p, it, sql, s3, bucket, pub), pend[i:i + 60]))
            feitos += len(pend[i:i + 60])
            gravar(p)
            limpar_fontes(p)
            log(f"--- {feitos}/{len(pend)}")
    gravar(p)
    status()


def status() -> None:
    p = json.loads(PLANO.read_text(encoding="utf-8"))
    print(dict(Counter(it["estado"] for it in p["itens"])))


def conferir() -> None:
    sql = banco()
    base = f"""from posts p join contas c on c.id = p.conta_id join midias m on m.id = p.midia_id
               where c.espaco_id = '{LUD}' and p.apagado_em is null and p.status = 'agendado'
                 and m.origem like 'espelho\\_l10/%'"""
    por = sql(f"""select c.username, (p.agendado_para at time zone 'America/Sao_Paulo')::date dia, count(*) n
                  {base} group by 1, 2 order by 1, 2""")
    tab = defaultdict(dict)
    for r in por:
        tab[r["username"]][r["dia"][5:]] = r["n"]
    for u, d in sorted(tab.items()):
        print(f"{u:20}", " ".join(f"{k}:{v}" for k, v in sorted(d.items())), "| total", sum(d.values()))
    print(sql(f"""select min(gap) min_gap_min from (
        select extract(epoch from p2.agendado_para - p1.agendado_para)/60 gap
          from (select p.conta_id, p.agendado_para, lead(p.agendado_para) over (partition by p.conta_id order by p.agendado_para) nx
                  from posts p join contas c on c.id = p.conta_id where c.espaco_id = '{LUD}' and p.apagado_em is null
                   and p.status in ('agendado', 'container_criado')) p1
          cross join lateral (select p1.nx agendado_para) p2 where p1.nx is not null) z"""))
    print(sql(f"""select max(n) max_contas_por_cena_dia from (select
        regexp_replace(m.cena_id, '^(l10|liso|lote)_(preto|branco)_(link_)?', '') sc,
        (p.agendado_para at time zone 'America/Sao_Paulo')::date d, count(distinct p.conta_id) n
        {base} group by 1, 2) z"""))
    print(sql(f"""select max(n) max_usos_cena, count(*) cenas from (select
        regexp_replace(m.cena_id, '^(l10|liso|lote)_(preto|branco)_(link_)?', '') sc, count(*) n {base} group by 1) z"""))
    print(sql(f"""select count(*) n, count(distinct m.url_video) urls, count(distinct m.url_capa) capas,
        min(p.agendado_para at time zone 'America/Sao_Paulo') ini, max(p.agendado_para at time zone 'America/Sao_Paulo') fim,
        count(*) filter (where p.legenda not like '%(@' || c.username || ')%') sem_arroba,
        count(*) filter (where p.legenda ilike '%comente%' or p.legenda like '%#%') ruim {base}"""))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1]
    if cmd == "plano":
        plano(*(int(a) for a in sys.argv[2:4]))
    elif cmd == "rodar":
        rodar(int(sys.argv[2]) if len(sys.argv) > 2 else 6)
    else:
        {"status": status, "conferir": conferir}[cmd]()
