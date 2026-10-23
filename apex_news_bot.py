"""Official Korean EA news first; free web translation only as a fallback.

Run --dry-run before configuring a NEW Discord webhook in .env.
No paid translation API, API key, Discord bot token, or Gateway connection.
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


BASE = Path(__file__).resolve().parent
EN_NEWS = 'https://www.ea.com/games/apex-legends/apex-legends/news'
KO_NEWS = 'https://www.ea.com/ko/games/apex-legends/apex-legends/news'
ARTICLE_PATH = re.compile(r'^/(?:[a-z]{2}(?:-[a-z]{2})?/)?games/apex-legends/(?:apex-legends/)?news/([^/]+)/?$')
LOG = logging.getLogger('apex-news')


@dataclass
class NewsRef:
    slug: str
    title: str
    url: str


@dataclass
class Article:
    slug: str
    title: str
    url: str
    body: str
    korean: bool
    image: str


class MissingArticle(ValueError):
    pass


class WaitingForKorean(RuntimeError):
    pass


class RateLimited(RuntimeError):
    def __init__(self, seconds):
        super().__init__('요청 제한: 잠시 후 재시도합니다.')
        self.seconds = max(1, float(seconds))


def slug_from_url(url):
    parsed = urlparse(url)
    match = ARTICLE_PATH.fullmatch(parsed.path)
    if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('www.ea.com', 'ea.com'):
        return None
    return match.group(1) if match else None


def parse_index(html, base_url):
    refs = {}
    for link in BeautifulSoup(html, 'html.parser').select('a[href]'):
        url = urljoin(base_url, link['href'])
        slug = slug_from_url(url)
        if slug and slug not in refs:
            url = url.split('#')[0].split('?')[0]
            refs[slug] = NewsRef(slug, link.get_text(' ', strip=True), url)
    if not refs:
        raise ValueError('뉴스 링크를 찾지 못했습니다. EA 페이지 구조/접근 상태를 확인하세요.')
    return list(refs.values())


def is_korean(text):
    hangul = len(re.findall(r'[가-힣]', text))
    letters = len(re.findall(r'[가-힣A-Za-z]', text))
    return hangul >= 15 and hangul / max(1, letters) >= 0.10


def parse_article(html, url, slug):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.find('main') or soup.find('article')
    heading = main.find('h1') if main else None
    canonical = soup.find('meta', property='og:url')
    if slug_from_url(url) != slug or not heading:
        raise MissingArticle('기사 주소가 홈페이지/다른 주소로 연결되었습니다.')
    if canonical and slug_from_url(canonical.get('content', '')) != slug:
        raise MissingArticle('요청한 기사와 페이지의 기사 ID가 다릅니다.')
    title = soup.find('meta', property='og:title')
    title = title.get('content', '').strip() if title else heading.get_text(' ', strip=True)
    image = soup.find('meta', property='og:image')
    image = urljoin(url, image.get('content', '')) if image else ''
    # Scope to article content when present. Modern EA uses main + BodyComponents.
    root = main.select_one('[class*="RichText_richText"], [itemprop="articleBody"]') or main
    for unwanted in root.select('script, style, nav, footer, aside, button, [class*="RelatedArticles"], [class*="ArticleCard"]'):
        unwanted.decompose()
    blocks = []
    for node in root.select('h2, h3, h4, h5, h6, p, li, table'):
        if node.find_parent('table') or (node.name == 'p' and node.find_parent('li')):
            continue
        if node.name == 'table':
            rows = [' | '.join(c.get_text(' ', strip=True) for c in row.find_all(['th', 'td']))
                    for row in node.find_all('tr')]
            text = '\n'.join(rows)
        elif node.name == 'li':
            # Skip nested lists here; their items are emitted separately below.
            fragment = BeautifulSoup(str(node), 'html.parser').li
            for nested in fragment.select('ul, ol'):
                nested.decompose()
            text = fragment.get_text(' ', strip=True)
            text = '- ' + text if text else ''
        else:
            text = node.get_text(' ', strip=True)
            if node.name.startswith('h') and text:
                text = '## ' + text
        if text and text not in ('맨 위로', 'Back to top'):
            blocks.append(text)
    body = '\n\n'.join(blocks).strip()
    if len(body) < 80:
        raise ValueError('기사 본문이 너무 짧습니다. 수집 오류일 수 있어 발송하지 않습니다.')
    if len(body) > 250_000:
        raise ValueError('비정상적으로 긴 본문입니다. EA 페이지 구조를 확인하세요.')
    lang = soup.html.get('lang', '').lower() if soup.html else ''
    korean = lang.startswith('ko') and is_korean(body)
    return Article(slug, title or heading.get_text(' ', strip=True), url, body, korean, image)


def get_html(session, url):
    response = session.get(url, timeout=(10, 30))
    if response.status_code == 429:
        raise RateLimited(response.headers.get('Retry-After', '600'))
    response.raise_for_status()
    response.encoding = 'utf-8'
    return response.text, response.url


def translation_chunks(text, limit=1800):
    chunks = []
    while text:
        end = min(limit, len(text))
        if end < len(text):
            boundary = max(text.rfind('\n\n', 0, end), text.rfind('. ', 0, end))
            if boundary > end // 2:
                end = boundary + (2 if text[boundary:boundary + 2] in ('\n\n', '. ') else 1)
        chunks.append(text[:end])
        text = text[end:]
    return chunks


def free_translate(text, session, pause=time.sleep):
    """Unofficial public web interface. No paid Cloud API or key is used.

    Failure/429 is propagated; no CAPTCHA bypass or alternate endpoints.
    Timeout is explicit (some translation wrappers omit a timeout).
    """
    translated = []
    for part in translation_chunks(text):
        pause(2)
        response = session.get('https://translate.google.com/m',
                               params={'sl': 'en', 'tl': 'ko', 'q': part}, timeout=(10, 30))
        if response.status_code == 429:
            raise RateLimited(600)
        response.raise_for_status()
        node = BeautifulSoup(response.text, 'html.parser').select_one('.result-container, .t0')
        if not node or not node.get_text().strip():
            raise RuntimeError('무료 번역 응답을 읽지 못했습니다. 다음 주기에 재시도합니다.')
        translated.append(node.get_text().strip())
    result = '\n\n'.join(translated)
    if not is_korean(result) and len(text) > 150:
        raise RuntimeError('번역 결과가 한국어로 확인되지 않습니다. 발송하지 않습니다.')
    return result


def resolve_article(ref, ko_refs, ea_session, translation_session, allow_free=True):
    ko_url = ko_refs[ref.slug].url if ref.slug in ko_refs else KO_NEWS + '/' + ref.slug
    try:
        html, final_url = get_html(ea_session, ko_url)
        article = parse_article(html, final_url, ref.slug)
        if article.korean:
            return article
    except requests.HTTPError as error:
        if error.response is None or error.response.status_code not in (404, 410):
            raise  # Temporary Korean-page errors must not trigger unnecessary translation.
    except MissingArticle:
        pass
    if not allow_free:
        raise WaitingForKorean('공식 한국어 기사를 기다립니다.')
    html, final_url = get_html(ea_session, EN_NEWS + '/' + ref.slug)
    article = parse_article(html, final_url, ref.slug)
    LOG.info('공식 한국어 없음: 무료 번역 중 (%s)', ref.slug)
    # Separate calls: the web translator may collapse line breaks. Never infer
    # the title/body boundary from translated output and accidentally drop text.
    title = free_translate(article.title, translation_session)
    body = free_translate(article.body, translation_session)
    return Article(ref.slug, title.strip(), article.url, body.strip(), False, article.image)


def clip_utf16(text, limit):
    return text.encode('utf-16-le')[:limit * 2].decode('utf-16-le', errors='ignore')


def preview_text(text, limit):
    """Short plain-text excerpt, safely displayed as Discord Markdown."""
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'([\\`*_~|\[\]<>])', r'\\\1', text)
    if len(text.encode('utf-16-le')) // 2 <= limit:
        return text
    shortened = clip_utf16(text, limit - 1)
    # Prefer a sentence boundary; never turn a clipped sentence into a claim
    # that looks complete. The full unchanged text stays in the attachment.
    boundaries = list(re.finditer(r'[.!?。](?:\s|$)', shortened))
    if boundaries and boundaries[-1].start() > len(shortened) // 2:
        shortened = shortened[:boundaries[-1].start() + 1]
    elif shortened.rfind(' ') > len(shortened) // 2:
        shortened = shortened[:shortened.rfind(' ')]
    return shortened.rstrip('\\ ').rstrip() + '…'


def article_preview(body):
    """Use publisher summary bullets, or headings; do not invent an AI summary."""
    summaries = {'요약', '핵심내용', '주요내용', '한눈에보기', '하이라이트',
                 'summary', 'tldr', 'highlights', 'ataglance', 'overview'}
    skip = summaries | {'인트로', '소개', '목차', '맨위로', 'intro', 'introduction',
                        'tableofcontents', 'backtotop', '패치노트', 'patchnotes'}
    intro, topics, bullets = '', [], []
    in_summary = False
    for block in re.split(r'\n\s*\n', body):
        block = block.strip()
        if not block:
            continue
        heading = re.fullmatch(r'#{1,6}\s+(.+)', block)
        if heading:
            title = heading.group(1).strip()
            key = re.sub(r'[^\w가-힣]', '', title).casefold()
            in_summary = key in summaries
            if key not in skip and title.casefold() not in {x.casefold() for x in topics}:
                topics.append(title)
            continue
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        is_list = all(re.match(r'^(?:[-*•]|\d+[.)])\s+', line) for line in lines)
        if is_list:
            if in_summary:
                for line in lines:
                    item = re.sub(r'^(?:[-*•]|\d+[.)])\s+', '', line)
                    if item not in bullets:
                        bullets.append(item)
            continue
        date_only = re.fullmatch(
            r'(?:\d{4}년\s*\d{1,2}월\s*\d{1,2}일|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|'
            r'[A-Za-z]+\.?\s+\d{1,2},?\s+\d{4})', block)
        if not intro and not block.startswith('#') and not date_only:
            intro = preview_text(block, 220)
    if bullets:
        return intro, '📌 주요 항목 · 원문 발췌', bullets[:5]
    return intro, '📑 이 기사에서 다루는 주제', topics[:5]


def build_message(article):
    label = 'EA 공식 한국어' if article.korean else '무료 자동 번역 · 비공식 번역'
    intro, section_name, items = article_preview(article.body)
    link = article.url.replace(')', '%29').replace('(', '%28')
    description = '\n\n'.join(part for part in (
        intro, f'🔗 [공식 원문 읽기]({link})\n📎 전체 본문은 첨부파일에서 확인하세요.'
    ) if part)
    embed = {'title': clip_utf16(article.title, 256), 'url': article.url,
             'author': {'name': 'APEX LEGENDS · NEWS'},
             'description': description, 'color': 15158332,
             'footer': {'text': 'Apex Legends News | ' + label}}
    if items:
        embed['fields'] = [{'name': section_name,
                            'value': '\n\n'.join('• ' + preview_text(item, 160) for item in items),
                            'inline': False}]
    if urlparse(article.image).scheme == 'https':
        embed['image'] = {'url': article.image}

    payload = {'embeds': [embed], 'allowed_mentions': {'parse': []}}
    attachment = f'{article.title}\n{label}\n원문: {article.url}\n\n{article.body}\n'
    return payload, attachment


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS seen (slug TEXT PRIMARY KEY, message_id TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS cached (slug TEXT PRIMARY KEY, article TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS pending (slug TEXT PRIMARY KEY, ref TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
        self.db.commit()

    def has_seen(self, slug):
        return self.db.execute('SELECT 1 FROM seen WHERE slug=?', (slug,)).fetchone() is not None

    def mark_seen(self, slug, message_id='baseline'):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO seen VALUES (?, ?)', (slug, str(message_id)))
            self.db.execute('DELETE FROM pending WHERE slug=?', (slug,))
            self.db.execute('DELETE FROM cached WHERE slug=?', (slug,))

    def message_id(self, slug):
        row = self.db.execute('SELECT message_id FROM seen WHERE slug=?', (slug,)).fetchone()
        return row[0] if row else None

    def cache(self, article):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cached VALUES (?, ?)',
                            (article.slug, json.dumps(asdict(article), ensure_ascii=False)))

    def cached(self, slug):
        row = self.db.execute('SELECT article FROM cached WHERE slug=?', (slug,)).fetchone()
        return Article(**json.loads(row[0])) if row else None

    def discover(self, refs):
        # Atomic first-run baseline: notify latest only, not the whole news archive.
        with self.db:
            if not self.db.execute("SELECT 1 FROM meta WHERE key='initialized'").fetchone():
                self.db.executemany('INSERT OR IGNORE INTO seen VALUES (?, ?)',
                                    [(ref.slug, 'baseline') for ref in refs[1:]])
                self.db.execute("INSERT INTO meta VALUES ('initialized', '1')")
            for ref in reversed(refs):
                if not self.has_seen(ref.slug):
                    self.db.execute('INSERT OR IGNORE INTO pending VALUES (?, ?)',
                                    (ref.slug, json.dumps(asdict(ref), ensure_ascii=False)))
        return [NewsRef(**json.loads(row[0])) for row in
                self.db.execute('SELECT ref FROM pending ORDER BY rowid')]

    def close(self):
        self.db.close()


def deliver_article(article, state, sender):
    state.cache(article)
    payload, text = build_message(article)
    message_id = sender(payload, text, article.slug)
    state.mark_seen(article.slug, message_id)


def send_webhook(session, webhook_url, payload, text, slug):
    filename = re.sub(r'[^A-Za-z0-9_-]', '_', slug)[:100] + '-ko.txt'
    response = session.post(webhook_url, params={'wait': 'true'},
                            data={'payload_json': json.dumps(payload, ensure_ascii=False)},
                            files={'files[0]': (filename, text.encode('utf-8-sig'), 'text/plain; charset=utf-8')},
                            timeout=(10, 60), allow_redirects=False)
    if response.status_code == 429:
        raise RateLimited(response.json().get('retry_after', 600))
    # Do not log response.url/body: the webhook token must never appear in logs.
    if response.status_code != 200:
        raise RuntimeError(f'디스코드 전송 실패: HTTP {response.status_code}')
    data = response.json()
    if not data.get('id'):
        raise RuntimeError('디스코드 메시지 ID가 확인되지 않았습니다.')
    return data['id']


def discover_news(session):
    english, korean = [], []
    for url, locale in ((EN_NEWS, 'en'), (KO_NEWS, 'ko')):
        try:
            html, final_url = get_html(session, url)
            refs = parse_index(html, final_url)
            if locale == 'en':
                english = refs
            else:
                korean = refs
        except RateLimited:
            raise
        except (requests.RequestException, ValueError) as error:
            LOG.warning('%s 목록 수집 실패 (%s)', locale, type(error).__name__)
    if not english and not korean:
        raise RuntimeError('양쪽 뉴스 목록을 읽지 못했습니다. 이번 확인은 건너뜁니다.')
    refs = {ref.slug: ref for ref in english}
    for ref in korean:
        refs.setdefault(ref.slug, ref)
    return list(refs.values()), {ref.slug: ref for ref in korean}


def new_session():
    session = requests.Session()
    session.headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) ApexNewsNotifier/1.0'
    return session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='공식 기사 1개를 확인만 합니다. 발송/DB 변경/번역 없음.')
    parser.add_argument('--once', action='store_true', help='한 번 확인하고 종료합니다. 실제 전송됩니다.')
    args = parser.parse_args()
    load_dotenv(BASE / '.env')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    interval = int(os.getenv('CHECK_INTERVAL_SECONDS', '600'))
    if interval < 60:
        raise SystemExit('CHECK_INTERVAL_SECONDS는 최소 60초, 권장 600초입니다.')
    allow_free = os.getenv('FREE_TRANSLATION', 'true').strip().lower() in ('true', '1', 'yes')
    webhook = os.getenv('DISCORD_WEBHOOK_URL', '').strip()
    if not args.dry_run and not re.fullmatch(r'https://(?:discord\.com|discordapp\.com)/api(?:/v\d+)?/webhooks/\d+/[\w.-]+', webhook):
        raise SystemExit('.env의 DISCORD_WEBHOOK_URL에 새로 발급한 웹훅 주소를 입력하세요.')
    ea, translator, discord = new_session(), new_session(), new_session()
    if args.dry_run:
        refs, ko_refs = discover_news(ea)
        ref = refs[0]
        try:
            article = resolve_article(ref, ko_refs, ea, translator, allow_free=False)
        except WaitingForKorean:
            LOG.info('기사 %d개 확인. 최신 기사 한국어 없음: 실행 시 무료 번역 대상 (%s)', len(refs), ref.slug)
            return
        LOG.info('기사 %d개 확인 | %s | 공식 한국어=%s | 본문 %d자',
                 len(refs), article.title, article.korean, len(article.body))
        LOG.info('점검 완료: 디스코드 전송, DB 변경, 번역 요청 없음.')
        return
    state = State(BASE / 'news_state.sqlite3')
    LOG.info('모니터링 시작: %d초 간격 / 무료 번역 %s / 종료 Ctrl+C', interval, allow_free)
    exit_code = 0
    try:
        while True:
            delay, failed = interval, False
            try:
                refs, ko_refs = discover_news(ea)
                pending = state.discover(refs)
                LOG.info('수집 %d개 / 미발송 %d개', len(refs), len(pending))
                for ref in pending:
                    try:
                        article = state.cached(ref.slug) or resolve_article(ref, ko_refs, ea, translator, allow_free)
                        deliver_article(article, state,
                                        lambda payload, text, slug: send_webhook(discord, webhook, payload, text, slug))
                        LOG.info('발송 성공: %s', article.title)
                        time.sleep(1)
                    except WaitingForKorean:
                        LOG.info('한국어 대기: %s', ref.slug)
                    except RateLimited:
                        raise
                    except Exception as error:
                        # requests exceptions can contain webhook URLs, so log type only.
                        failed = True
                        LOG.warning('미발송 유지: %s (%s)', ref.slug, safe_error(error))
            except RateLimited as error:
                failed, delay = True, max(interval, error.seconds)
                LOG.warning('요청 제한: %.0f초 뒤 재시도', delay)
            except Exception as error:
                failed = True
                LOG.warning('이번 확인 실패 (%s)', safe_error(error))
            if args.once:
                exit_code = int(failed)
                break
            time.sleep(delay)
    except KeyboardInterrupt:
        LOG.info('종료합니다. 전송 기록은 보존됩니다.')
    finally:
        state.close()
        ea.close()
        translator.close()
        discord.close()
    return exit_code


def safe_error(error):
    if isinstance(error, requests.RequestException):
        return type(error).__name__
    return re.sub(r'https://\S+', '[URL 숨김]', str(error))[:240]


if __name__ == '__main__':
    raise SystemExit(main())
