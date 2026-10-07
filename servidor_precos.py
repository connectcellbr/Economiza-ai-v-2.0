#!/usr/bin/env python3
"""
Economiza AI — Servidor local de preços reais
======================================
Roda a busca no Zoom e devolve JSON para o painel HTML.

Uso:
  python servidor_precos.py

Depois abra o painel: http://127.0.0.1:8765/
"""

from __future__ import annotations

import base64
import io
import json
import re
import subprocess
import tempfile
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from auto_aprendizado import record_event, update_metric, snapshot, learn, propose_improvements, get_status
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from PIL import Image

DEFAULT_PORT = 8765
PORT = int(__import__("os").environ.get("PORT", __import__("os").environ.get("PRECO_PORT", DEFAULT_PORT)))
DIR = Path(__file__).resolve().parent

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def parse_price(price_str: str) -> Optional[float]:
    if not price_str:
        return None
    cleaned = re.sub(r"[R$\s]", "", price_str)
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_installments(text: str) -> str:
    m = re.search(r"(\d+\s*x\s*de\s*R\$\s*[\d.,]+)(?:\s*sem\s*juros)?", text, re.I)
    if m:
        result = m.group(0).strip()
        if "sem juros" in text.lower() and "sem juros" not in result.lower():
            result += " sem juros"
        return result
    return ""


def extract_cashback(text: str) -> str:
    m = re.search(r"(\d+%\s*de\s*volta)", text, re.I)
    if m:
        return m.group(1)
    if "cashback" in text.lower():
        return "Cashback disponível"
    return ""


def is_new_product(name: str, text: str) -> bool:
    lower = (name + " " + text).lower()
    return not any(w in lower for w in ["usado", "seminovo", "recondicionado", "open box"])


def search_zoom(product: str, max_results: int = 15) -> List[Dict[str, Any]]:
    query = urllib.parse.quote_plus(product)
    url = f"https://www.zoom.com.br/search?q={query}"

    try:
        response = requests.get(url, headers=HEADERS, timeout=18)
        response.raise_for_status()
    except requests.RequestException as e:
        return {"error": str(e), "results": []}  # type: ignore

    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.select('article[class*="OrqProductCard"]')

    results: List[Dict[str, Any]] = []
    keywords = [k.lower() for k in product.split() if len(k) > 2]

    for card in cards[: max_results * 3]:
        full_text = card.get_text(separator=" | ", strip=True)

        name_el = card.select_one('[class*="Name_OrqProductCard_Name"]')
        name = name_el.get_text(strip=True) if name_el else "Nome não encontrado"

        price_text = None
        for s in card.find_all(string=re.compile(r"^R\$\s*[\d.,]+$")):
            parent_text = s.parent.get_text() if s.parent else ""
            if "x de" in parent_text.lower():
                continue
            price_text = s.strip()
            break
        if not price_text:
            matches = re.findall(r"R\$\s*[\d.,]+", full_text)
            for m in matches:
                if "x de" not in full_text.lower().split(m)[0][-25:]:
                    price_text = m
                    break

        price_value = parse_price(price_text) if price_text else None
        if price_value is None:
            continue

        store = "—"
        via = card.find(string=re.compile(r"Via\s+.+"))
        if via:
            store = re.sub(r"^Via\s+", "", via.strip()).strip()
            store = re.split(r"\s{2,}|\|", store)[0].strip()

        link_el = card.select_one("a[href]")
        href = link_el.get("href", "") if link_el else ""
        link = ("https://www.zoom.com.br" + href) if href.startswith("/") else href

        rating = "—"
        rating_match = re.search(r"(\d(?:\.\d)?)\s*\(\d+\)", full_text)
        if rating_match:
            rating = rating_match.group(1)

        installments = extract_installments(full_text)
        cashback = extract_cashback(full_text)

        shipping_text = "A calcular"
        shipping_cost = None
        if "frete grátis" in full_text.lower() or "frete gratis" in full_text.lower():
            shipping_text = "Grátis"
            shipping_cost = 0.0

        is_new = is_new_product(name, full_text)

        name_lower = name.lower()
        relevance = sum(1 for k in keywords if k in name_lower)
        main_phrase = " ".join(keywords[:2]) if len(keywords) >= 2 else (keywords[0] if keywords else "")
        if main_phrase and main_phrase in name_lower:
            relevance += 3
        if is_new:
            relevance += 1

        results.append(
            {
                "name": name,
                "price": price_value,
                "priceText": price_text or "—",
                "store": store,
                "link": link or url,
                "rating": rating,
                "isNew": is_new,
                "installments": installments,
                "cashback": cashback,
                "shippingText": shipping_text,
                "shippingCost": shipping_cost,
                "freeShip": shipping_cost == 0,
                "_relevance": relevance,
            }
        )

    # Filtro de relevância
    if keywords:
        important = keywords[:2] if len(keywords) >= 2 else keywords
        filtered = [r for r in results if all(k in r["name"].lower() for k in important)]
        if len(filtered) >= 2:
            results = filtered
        else:
            min_rel = max(1, (len(keywords) + 1) // 2)
            results = [r for r in results if r.get("_relevance", 0) >= min_rel]

    results.sort(key=lambda o: (-o.get("_relevance", 0), o["price"]))
    for r in results:
        r.pop("_relevance", None)

    return results[:max_results]




# ================= PESQUISA WEB GERAL =================
def _clean_text(text: str) -> str:
    return re.sub(r"\\s+", " ", BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)).strip()


def _search_html_engine(url: str, source: str, max_results: int = 8) -> List[Dict[str, Any]]:
    """Consulta um mecanismo público de busca com tratamento uniforme."""
    r = requests.get(url, headers=HEADERS, timeout=7, allow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []

    if source == "DuckDuckGo":
        items = soup.select(".result")
        for item in items[:max_results]:
            a = item.select_one(".result__a")
            snippet_el = item.select_one(".result__snippet")
            if not a:
                continue
            out.append({
                "title": a.get_text(" ", strip=True),
                "url": a.get("href", ""),
                "snippet": snippet_el.get_text(" ", strip=True) if snippet_el else "",
                "source": source,
            })
    elif source == "Bing":
        items = soup.select("li.b_algo")
        for item in items[:max_results]:
            a = item.select_one("h2 a")
            snippet_el = item.select_one(".b_caption p")
            if not a:
                continue
            out.append({
                "title": a.get_text(" ", strip=True),
                "url": a.get("href", ""),
                "snippet": snippet_el.get_text(" ", strip=True) if snippet_el else "",
                "source": source,
            })
    elif source == "Google":
        # Estrutura simples e tolerante; não depende de classes que mudam com frequência.
        for a in soup.select("a"):
            href = a.get("href", "")
            title = a.get_text(" ", strip=True)
            if not href.startswith("http") or not title or len(title) < 4:
                continue
            host = urllib.parse.urlparse(href).netloc.lower()
            if "google." in host:
                continue
            out.append({"title": title, "url": href, "snippet": "", "source": source})
            if len(out) >= max_results:
                break
    elif source == "Yahoo":
        for item in soup.select("div.algo")[:max_results]:
            a = item.select_one("h3 a, h3.title a, a")
            if not a:
                continue
            href = a.get("href", "")
            title = a.get_text(" ", strip=True)
            snippet_el = item.select_one("div.compText, p")
            if href and title:
                out.append({
                    "title": title,
                    "url": href,
                    "snippet": snippet_el.get_text(" ", strip=True) if snippet_el else "",
                    "source": source,
                })
    return [x for x in out if x.get("url")]


def search_duckduckgo(query: str, max_results: int = 8) -> List[Dict[str, Any]]:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
    return _search_html_engine(url, "DuckDuckGo", max_results)


def search_bing(query: str, max_results: int = 8) -> List[Dict[str, Any]]:
    url = "https://www.bing.com/search?q=" + urllib.parse.quote_plus(query)
    return _search_html_engine(url, "Bing", max_results)


def search_google(query: str, max_results: int = 8) -> List[Dict[str, Any]]:
    url = "https://www.google.com/search?hl=pt-BR&num=10&q=" + urllib.parse.quote_plus(query)
    return _search_html_engine(url, "Google", max_results)


def search_yahoo(query: str, max_results: int = 8) -> List[Dict[str, Any]]:
    url = "https://search.yahoo.com/search?p=" + urllib.parse.quote_plus(query)
    return _search_html_engine(url, "Yahoo", max_results)


def web_search(query: str, max_results: int = 12) -> Dict[str, Any]:
    """Pesquisa ampla em mecanismos públicos. Usa vários fallbacks e nunca fabrica resultados."""
    engines = []
    errors = []
    # Consulta as fontes em paralelo para que uma fonte lenta/bloqueada não congele a pesquisa.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    fns = (search_duckduckgo, search_bing, search_google, search_yahoo)
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(fn, query, max_results): fn.__name__ for fn in fns}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                engines.extend(fut.result())
            except Exception as exc:
                errors.append(f"{name}: {exc}")

    seen = set()
    results = []
    for item in engines:
        raw = item.get("url", "")
        key = raw.split("#", 1)[0].rstrip("/")
        if not key or key in seen:
            continue
        seen.add(key)
        results.append(item)
        if len(results) >= max_results:
            break

    return {
        "ok": bool(results),
        "query": query,
        "count": len(results),
        "results": results,
        "engines": sorted(set(x.get("source") for x in engines if x.get("source"))),
        "errors": errors,
    }


def _domain_store(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
    mapping = {
        "mercadolivre.com.br": "Mercado Livre",
        "amazon.com.br": "Amazon Brasil",
        "magazineluiza.com.br": "Magazine Luiza",
        "magalu.com": "Magazine Luiza",
        "casasbahia.com.br": "Casas Bahia",
        "kabum.com.br": "KaBuM!",
        "shopee.com.br": "Shopee",
        "carrefour.com.br": "Carrefour",
        "extra.com.br": "Extra",
        "pontofrio.com.br": "Ponto",
        "americanas.com.br": "Americanas",
        "fastshop.com.br": "Fast Shop",
        "zoom.com.br": "Zoom",
    }
    return mapping.get(host, host)


def _prices_from_text(text: str) -> List[float]:
    vals = []
    for m in re.finditer(r"R\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|[0-9]+(?:,[0-9]{2})?)", text or "", re.I):
        prefix = (text[max(0, m.start()-24):m.start()] or "").lower()
        # Não confundir parcela (ex.: 12x de R$ 477,67) com preço à vista.
        if re.search(r"\d+\s*x\s*de\s*$", prefix):
            continue
        try:
            vals.append(float(m.group(1).replace('.', '').replace(',', '.')))
        except ValueError:
            pass
    return vals


def _score_product_match(query: str, title: str) -> int:
    q = [x.lower() for x in re.findall(r"[a-z0-9]+", query) if len(x) > 2]
    t = (title or '').lower()
    return sum(1 for x in q if x in t)


def search_multifonte(product: str, max_results: int = 24) -> Dict[str, Any]:
    """Coleta ofertas de múltiplas lojas usando páginas públicas/indexadas.
    Não inventa preços: só aceita valores explicitamente encontrados no texto.
    """
    store_domains = [
        ("Mercado Livre", "site:mercadolivre.com.br"),
        ("Amazon Brasil", "site:amazon.com.br"),
        ("Magazine Luiza", "site:magazineluiza.com.br OR site:magalu.com"),
        ("Casas Bahia", "site:casasbahia.com.br"),
        ("KaBuM!", "site:kabum.com.br"),
        ("Shopee", "site:shopee.com.br"),
    ]
    collected: List[Dict[str, Any]] = []
    errors: List[str] = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    def one(store, domain):
        q = f'{domain} {product}'
        return store, web_search(q, max_results=6)
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(one, *x) for x in store_domains]
        for f in as_completed(futs):
            try:
                store, data = f.result()
                errors.extend(data.get('errors', []))
                for item in data.get('results', []):
                    if store.lower() not in item.get('title','').lower() and _domain_store(item.get('url','')) != store:
                        # Mantém apenas resultados pertencentes ao domínio pedido.
                        continue
                    text = f"{item.get('title','')} {item.get('snippet','')}"
                    prices = _prices_from_text(text)
                    if not prices:
                        continue
                    price = min(x for x in prices if x > 0)
                    title = item.get('title','').strip()
                    relevance = _score_product_match(product, title)
                    if relevance <= 0:
                        continue
                    collected.append({
                        'name': title,
                        'price': price,
                        'priceText': f'R$ {price:,.2f}'.replace(',', 'X').replace('.', ',').replace('X','.'),
                        'store': store,
                        'link': item.get('url',''),
                        'rating': '—',
                        'isNew': True,
                        'installments': '',
                        'cashback': '',
                        'shippingText': 'A calcular',
                        'shippingCost': None,
                        'freeShip': False,
                        'source': item.get('source','web'),
                        '_relevance': relevance,
                    })
            except Exception as exc:
                errors.append(str(exc))

    # Inclui Zoom como fonte adicional, quando acessível.
    try:
        zoom = search_zoom(product, max_results=12)
        if isinstance(zoom, list):
            for item in zoom:
                item = dict(item)
                item['source'] = 'Zoom'
                item['_relevance'] = _score_product_match(product, item.get('name',''))
                if item['_relevance'] > 0:
                    collected.append(item)
    except Exception as exc:
        errors.append(f'Zoom: {exc}')

    # Deduplicação por loja + nome aproximado; prioriza maior relevância e menor preço.
    best_by_key = {}
    for item in collected:
        key = (item.get('store','').lower(), re.sub(r'[^a-z0-9]+','',item.get('name','').lower())[:100])
        old = best_by_key.get(key)
        if old is None or (item.get('_relevance',0), -item['price']) > (old.get('_relevance',0), -old['price']):
            best_by_key[key] = item
    results = list(best_by_key.values())
    results.sort(key=lambda x: (-x.get('_relevance',0), x.get('price', float('inf'))))
    stores = sorted({x.get('store') for x in results if x.get('store')})
    for x in results:
        x.pop('_relevance', None)
    return {'ok': bool(results), 'query': product, 'count': len(results), 'results': results[:max_results], 'stores': stores, 'errors': errors[:10]}


def escolher_melhor(results: List[Dict]) -> Optional[Dict]:
    if not results:
        return None

    def score(o: Dict) -> float:
        s = float(o["price"])
        if not o.get("isNew", True):
            s += 80
        if o.get("shippingCost") is None:
            s += 8
        if o.get("shippingCost") == 0:
            s -= 5
        if o.get("cashback"):
            s -= 3
        inst = (o.get("installments") or "").lower()
        if "sem juros" in inst:
            s -= 3
        return s

    return min(results, key=score)



def identify_product_from_image(image_bytes: bytes) -> Dict[str, Any]:
    """
    Identifica produto a partir de imagem (otimizado).
    - Pré-processamento de imagem para OCR
    - Múltiplos modos Tesseract
    - Extração inteligente de nome de produto
    """
    suggestions: List[str] = []
    ocr_text = ""

    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        # Limita tamanho
        max_side = 1600
        if max(img.size) > max_side:
            ratio = max_side / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.Resampling.LANCZOS)

        # Versão em escala de cinza + contraste para OCR
        gray = img.convert("L")
        # Aumenta contraste simples
        from PIL import ImageOps, ImageFilter, ImageEnhance
        gray = ImageOps.autocontrast(gray, cutoff=2)
        gray = ImageEnhance.Sharpness(gray).enhance(1.4)
        gray = gray.filter(ImageFilter.MedianFilter(size=3))

        texts = []
        with tempfile.TemporaryDirectory() as td:
            p_color = str(Path(td) / "color.png")
            p_gray = str(Path(td) / "gray.png")
            img.save(p_color, format="PNG")
            gray.save(p_gray, format="PNG")

            # Tenta vários PSM (modos de segmentação)
            for img_path in (p_gray, p_color):
                for psm in ("6", "11", "4", "3"):
                    try:
                        result = subprocess.run(
                            ["tesseract", img_path, "stdout", "-l", "por+eng", f"--psm", psm],
                            capture_output=True,
                            text=True,
                            timeout=20,
                        )
                        t = (result.stdout or "").strip()
                        if t and len(t) > len(max(texts, default="")):
                            texts.append(t)
                    except Exception:
                        continue
                    # Se já tem texto bom, para cedo
                    if texts and len(max(texts, key=len)) > 40:
                        break
                if texts and len(max(texts, key=len)) > 40:
                    break

        ocr_text = max(texts, key=len) if texts else ""

        if not ocr_text:
            return {
                "ok": False,
                "query": "",
                "suggestions": [],
                "ocr_preview": "",
                "method": "ocr",
                "message": "Não consegui ler texto na imagem. Tire foto mais nítida da embalagem/rótulo, ou digite o nome do produto.",
            }

        # Limpa linhas
        lines = []
        for ln in ocr_text.splitlines():
            ln = re.sub(r"\s+", " ", ln).strip()
            if len(ln) < 3:
                continue
            # Remove linhas só com ruído/números isolados
            if re.fullmatch(r"[\d\s.,R$%€/\-|:]+", ln):
                continue
            # Remove caracteres estranhos demais
            clean = re.sub(r"[^\w\s\-./+&áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ]", " ", ln, flags=re.I)
            clean = re.sub(r"\s+", " ", clean).strip()
            if len(clean) >= 3:
                lines.append(clean)

        brands = [
            "samsung", "apple", "iphone", "xiaomi", "motorola", "lg", "sony", "nokia",
            "nike", "adidas", "puma", "olympikus", "mizuno", "fila",
            "dell", "lenovo", "hp", "asus", "acer", "positivo", "vaio",
            "philips", "mondial", "britânia", "britania", "electrolux", "brastemp", "consul",
            "multilaser", "jbl", "logitech", "razer", "canon", "nikon", "gopro",
            "tramontina", "tramotina", "tramont", "tramontini",
            "cabo", "carregador", "fone", "headset", "mouse", "teclado", "ssd", "hd",
            "air fryer", "fritadeira", "liquidificador", "cafeteira", "ferro",
        ]

        scored = []
        for ln in lines:
            low = ln.lower()
            score = 0
            # Bônus por marca conhecida
            if any(b in low for b in brands):
                score += 5
            # Bônus por ter números (modelo/capacidade)
            if re.search(r"\d", ln):
                score += 2
            # Bônus por tamanho médio (nome de produto típico)
            words = ln.split()
            if 2 <= len(words) <= 10:
                score += 3
            elif len(words) == 1 and len(ln) > 4:
                score += 1
            # Penaliza linhas muito longas (parágrafos)
            if len(ln) > 90:
                score -= 3
            scored.append((score, ln))

        scored.sort(key=lambda x: -x[0])
        suggestions = []
        seen = set()
        for sc, ln in scored:
            key = ln.lower()
            if key in seen:
                continue
            seen.add(key)
            suggestions.append(ln)
            if len(suggestions) >= 8:
                break

        query = suggestions[0] if suggestions else ""
        # Refina query: remove prefixos comuns de ruído OCR
        if query:
            query = re.sub(r"^(www\.|http).*", "", query, flags=re.I).strip()
            # Correções comuns de OCR em produtos BR
            fixes = {
                r"\bFic\b": "Fio",
                r"\bFlO\b": "Fio",
                r"\bSem Fic\b": "Sem Fio",
                r"\bBluet00th\b": "Bluetooth",
                r"\bBlueto0th\b": "Bluetooth",
                r"\bBluetooth\b": "Bluetooth",
                r"\bSamsunq\b": "Samsung",
                r"\bSamsunq\b": "Samsung",
                r"\bLogltech\b": "Logitech",
                r"\bXlaomi\b": "Xiaomi",
                r"\b1ph0ne\b": "iPhone",
                r"\blPhone\b": "iPhone",
                r"\b0\b(?=\s*GB)": "0",
            }
            for pat, rep in fixes.items():
                query = re.sub(pat, rep, query, flags=re.I)
            query = query[:90]
            # Aplica nas suggestions também
            suggestions = [re.sub(r"\bSem Fic\b", "Sem Fio", s, flags=re.I) for s in suggestions]

        return {
            "ok": bool(query),
            "query": query,
            "suggestions": suggestions,
            "ocr_preview": ocr_text[:600],
            "method": "ocr",
            "message": (
                f'Identifiquei: "{query}"' if query
                else "Li algo na imagem, mas não consegui montar o nome do produto. Tente outra foto ou digite o nome."
            ),
        }
    except Exception as e:
        return {
            "ok": False,
            "query": "",
            "suggestions": [],
            "ocr_preview": "",
            "method": "error",
            "message": f"Erro ao processar imagem: {e}",
        }



# ================= AGENTE AUTÔNOMO =================
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def infer_intent(question: str) -> Dict[str, Any]:
    q = _norm(question).lower()
    shopping = bool(re.search(r"\b(pre[cç]o|comprar|compra|oferta|promo[cç][aã]o|barato|mais barato|quanto custa|onde comprar|frete|parcel|produto|celular|notebook|fone|tv|geladeira|air fryer)\b", q))
    current = bool(re.search(r"\b(hoje|agora|atual|atualmente|esta semana|2026|2025|not[ií]cia|lan[cç]amento|vers[aã]o nova)\b", q))
    howto = bool(re.search(r"\b(como|passo a passo|ensine|tutorial|configurar|instalar|resolver|consertar)\b", q))
    compare = bool(re.search(r"\b(compar|diferen[cç]a|melhor|qual escolher|qual vale|versus|vs\.?|entre)\b", q))
    return {"shopping": shopping, "current": current, "howto": howto, "compare": compare}


def build_plan(question: str) -> List[Dict[str, Any]]:
    intent = infer_intent(question)
    q = _norm(question)
    plan = [{"tool": "web_search", "query": q, "reason": "pesquisa principal"}]
    if intent["current"]:
        plan.append({"tool": "web_search", "query": q + " fontes oficiais notícias recentes", "reason": "validar atualidade"})
    if intent["shopping"]:
        plan.append({"tool": "price_search", "query": q, "reason": "verificar preços reais"})
    if intent["compare"]:
        plan.append({"tool": "web_search", "query": q + " comparação especificações avaliações", "reason": "cruzar critérios"})
    return plan[:4]


def fetch_page(url: str, max_chars: int = 7000) -> Dict[str, Any]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if "text/html" not in ct and "text/plain" not in ct:
            return {"ok": False, "url": url, "error": "conteúdo não textual"}
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
            tag.decompose()
        title = soup.title.get_text(" ", strip=True) if soup.title else url
        text = _clean_text(soup.get_text(" ", strip=True))
        return {"ok": True, "url": r.url, "title": title[:240], "text": text[:max_chars]}
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}


def _keywords(question: str) -> List[str]:
    stop = {"para","como","qual","quais","onde","quando","porque","porquê","sobre","isso","esse","essa","com","uma","mais","menos","que","dos","das","de","do","da","e","ou","em","no","na","um","uma","eu","você","voce","me","the","and","for"}
    return [w for w in re.findall(r"[\wÀ-ÿ]+", question.lower()) if len(w) > 2 and w not in stop][:14]


def rank_evidence(question: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    kws = _keywords(question)
    ranked=[]
    for item in items:
        text = (item.get("title","") + " " + item.get("snippet","") + " " + item.get("text","")).lower()
        score = sum(text.count(k) for k in kws)
        if item.get("source"): score += 1
        item = dict(item); item["evidence_score"] = score
        ranked.append(item)
    return sorted(ranked, key=lambda x: x.get("evidence_score",0), reverse=True)


def synthesize_agent(question: str, sources: List[Dict[str, Any]], prices: List[Dict[str, Any]], intent: Dict[str, Any]) -> Dict[str, Any]:
    ranked = rank_evidence(question, sources)
    unique=[]; seen=set()
    for x in ranked:
        u=x.get("url","")
        if u and u not in seen:
            seen.add(u); unique.append(x)
    unique=unique[:6]
    parts=[]
    if prices:
        ordered=sorted(prices, key=lambda x: x.get("price", float("inf")))
        best=ordered[0]
        parts.append(f"Encontrei {len(prices)} ofertas reais. A menor encontrada é {best.get('priceText','—')} em {best.get('store','—')}.")
        if intent.get("compare") and len(ordered)>1:
            parts.append("Outras opções: " + "; ".join(f"{x.get('store','—')} — {x.get('priceText','—')}" for x in ordered[1:4]) + ".")
    if unique:
        # Síntese factual conservadora baseada nos snippets/textos extraídos.
        snippets=[]
        for x in unique[:4]:
            snippet=_norm(x.get("snippet") or x.get("text") or "")
            if snippet: snippets.append(f"{x.get('title','Fonte')}: {snippet[:420]}")
        if snippets:
            parts.append("Principais evidências encontradas: " + " | ".join(snippets))
    if not parts:
        parts.append("Não consegui obter evidências suficientes para responder com segurança. Posso tentar outra estratégia de pesquisa.")
    return {"answer": "<br><br>".join(parts), "sources": unique, "confidence": "alta" if len(unique)>=3 else ("média" if unique else "baixa")}


def run_agent(question: str) -> Dict[str, Any]:
    question = _norm(question)
    if not question:
        return {"ok": False, "answer": "Faça uma pergunta para eu começar.", "plan": [], "sources": []}
    intent=infer_intent(question)
    plan=build_plan(question)
    web_queries=[x["query"] for x in plan if x["tool"]=="web_search"]
    price_queries=[x["query"] for x in plan if x["tool"]=="price_search"]
    all_web=[]; errors=[]
    # Executa sub-tarefas em paralelo para reduzir latência.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs=[ex.submit(web_search,q,8) for q in web_queries]
        for f in as_completed(futs):
            try:
                d=f.result(); all_web.extend(d.get("results",[])); errors.extend(d.get("errors",[]))
            except Exception as e: errors.append(str(e))
    # Remove duplicados e visita algumas fontes para ampliar o contexto.
    dedup=[]; seen=set()
    for x in all_web:
        u=x.get("url","").split("#",1)[0]
        if u and u not in seen:
            seen.add(u); dedup.append(x)
    pages=[]
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs=[ex.submit(fetch_page,x["url"]) for x in dedup[:6] if x.get("url")]
        for f in as_completed(futs):
            try:
                d=f.result()
                if d.get("ok"): pages.append(d)
            except Exception as e: errors.append(str(e))
    sources=[]
    for x in dedup[:8]:
        merged=dict(x)
        match=next((p for p in pages if p.get("url")==x.get("url")),None)
        if match: merged.update(match)
        sources.append(merged)
    prices=[]
    for q in price_queries[:1]:
        try:
            r=search_multifonte(q, max_results=24)
            if isinstance(r, dict): r=r.get("results", [])
            if isinstance(r,list): prices=r
        except Exception as e: errors.append(str(e))
    result=synthesize_agent(question,sources,prices,intent)
    return {"ok": bool(sources or prices), "question":question, "intent":intent, "plan":plan, "sources":result["sources"], "prices":prices[:8], "answer":result["answer"], "confidence":result["confidence"], "errors":errors[:6]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[Economiza AI] {args[0]}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        if path == "/api/learn":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                self._json(200, learn(payload))
            except Exception as e:
                self._json(400, {"ok": False, "error": str(e)})
            return

        if path == "/api/identify":
            try:
                data = json.loads(body.decode("utf-8"))
                b64 = data.get("image", "")
                if "," in b64:
                    b64 = b64.split(",", 1)[1]
                image_bytes = base64.b64decode(b64)
                result = identify_product_from_image(image_bytes)
                self._json(200, result)
            except Exception as e:
                self._json(400, {"ok": False, "message": str(e), "query": "", "suggestions": []})
            return

        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # API de busca
        if path == "/api/search":
            q = (qs.get("q") or [""])[0].strip()
            if not q:
                self._json(400, {"error": "Parâmetro q obrigatório", "results": []})
                return
            print(f"Buscando: {q}")
            data = search_multifonte(q, max_results=24)
            results = data.get("results", [])
            best = escolher_melhor(results)
            self._json(200 if data.get("ok") else 502, {
                "query": q,
                "count": len(results),
                "results": results,
                "best": best,
                "source": "multifonte",
                "stores": data.get("stores", []),
                "errors": data.get("errors", []),
            })
            return

        # Pesquisa ampla na web
        if path == "/api/web-search":
            q = (qs.get("q") or [""])[0].strip()
            if not q:
                self._json(400, {"ok": False, "error": "Parâmetro q obrigatório", "results": []})
                return
            print(f"Pesquisa web: {q}")
            data = web_search(q, max_results=12)
            if not data["ok"]:
                self._json(502, data)
                return
            self._json(200, data)
            return

        # Agente autônomo: planeja, pesquisa, cruza fontes, consulta preços e sintetiza.
        if path == "/api/agent":
            q = (qs.get("q") or [""])[0].strip()
            if not q:
                self._json(400, {"ok": False, "error": "Parâmetro q obrigatório"})
                return
            print(f"Agente: {q}")
            data = run_agent(q)
            if data.get("ok"):
                update_metric("agent_success")
            else:
                update_metric("agent_errors")
            self._json(200 if data.get("ok") else 502, data)
            return

        # Autoaprendizado controlado
        if path == "/api/learn":
            try:
                payload = json.loads((qs.get("data") or ["{}"])[0])
            except Exception:
                payload = {"kind": "feedback", "text": (qs.get("text") or [""])[0]}
            self._json(200, learn(payload))
            return

        if path == "/api/self-improve":
            action = (qs.get("action") or ["status"])[0]
            if action == "checkpoint":
                self._json(200, snapshot("auto-checkpoint"))
            elif action == "propose":
                self._json(200, propose_improvements())
            else:
                self._json(200, get_status())
            return

        if path == "/api/health":
            self._json(200, {"ok": True, "service": "Economiza AI", "port": PORT, "web_search": True, "price_search": True, "image_ocr": True})
            return

        # Arquivos estáticos
        if path == "/" or path == "":
            path = "/economiza_ai_painel.html"

        file_path = (DIR / path.lstrip("/")).resolve()
        if not str(file_path).startswith(str(DIR)) or not file_path.is_file():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        content_types = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript",
            ".css": "text/css",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".py": "text/plain",
        }
        ctype = content_types.get(file_path.suffix.lower(), "application/octet-stream")
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)


def main():
    # Injeta no HTML a URL da API real (mesma origem)
    html_path = DIR / "economiza_ai_painel.html"
    if html_path.exists():
        html = html_path.read_text(encoding="utf-8")
        if "USE_REAL_API" not in html:
            # marca será usada no JS
            pass

    global PORT
    requested = PORT
    last_error = None
    for candidate in range(requested, requested + 20):
        try:
            server = HTTPServer(("0.0.0.0", candidate), Handler)
            PORT = candidate
            break
        except OSError as exc:
            last_error = exc
    else:
        raise RuntimeError(f"Não foi possível abrir uma porta entre {requested} e {requested + 19}: {last_error}")

    print("=" * 50)
    print("  Economiza AI — Servidor de preços REAIS")
    print("=" * 50)
    print(f"  Abra no navegador:")
    print(f"  → http://127.0.0.1:{PORT}/")
    print()
    print("  API: http://127.0.0.1:{}/api/search?q=produto".format(PORT))
    print("  Ctrl+C para parar")
    print("=" * 50)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
        server.server_close()


if __name__ == "__main__":
    main()
