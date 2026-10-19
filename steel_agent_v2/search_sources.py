"""BR-007: verified-result membership, compact presentation, no network or intent router."""
from urllib.parse import urlsplit
import re

from .contracts import PresentationRun
from .telegram_format import prose, literal, safe_link
from .transport import safe_source


URL=re.compile(r'https?://[^\s<>"`\\]+')
OPAQUE=re.compile(r'\ue200cite\ue202[^\ue201]*\ue201|【\d+†[^】]+】')
REFERENCE=re.compile(r'(?:\[\d+\]|\ue200cite\ue202[^\ue201]*\ue201|【\d+†[^】]+】)')
HEADINGS={'источники','ссылки','sources','references'}


def merge_sources(sources):
    """Exact URLs only. Preserve alternate titles, origins and citation provenance."""
    result={}
    for source in sources:
        if not safe_source(source.url):continue
        old=result.get(source.url)
        titles=list(dict.fromkeys([*(old.titles if old else []),*([old.title] if old and old.title!=old.url else []),
                                  *source.titles,*([source.title] if source.title!=source.url else [])]))
        if old is None:result[source.url]=source.model_copy(update={'titles':titles});continue
        prefer=source.title if source.title!=source.url and (
            old.title==old.url or 'url_citation' in source.origins and 'url_citation' not in old.origins) else old.title
        result[source.url]=old.model_copy(update={
            'title':prefer,'titles':titles,'origins':list(dict.fromkeys([*old.origins,*source.origins])),
            'citations':old.citations+[c for c in source.citations if c not in old.citations]})
    return list(result.values())


def label(source):
    title=' '.join(source.title.split())
    # A hostname comes from the actual saved URL; do not invent a site/brand name.
    if title and title!=source.url and not URL.search(title) and len(title)<=64:return title
    return urlsplit(source.url).hostname or source.url


def _url_value(value,known):
    return value if value in known else value.rstrip('.,;:!?)]}')


def select(sources,text):
    inline={run.url for run in prose(text) if run.url}
    inline.update(_url_value(match[0],{s.url for s in sources}) for match in URL.finditer(text))
    def priority(source):
        if 'url_citation' in source.origins:return 0 if source.citations else 1
        if source.url in inline:return 2
        if any(origin in source.origins for origin in ('open_page','find_in_page')):return 3
        return 4
    return [source for source in sorted(sources,key=priority) if safe_link(source.url)][:2]


def _protect(text,literals):
    values=sorted({line for value in literals if value for line in value.splitlines() if line},key=lambda v:(-len(v),v))
    prefix='\ue000source-data:'
    while prefix in text:prefix+=':'
    saved={}
    def replace(match):
        key=prefix+str(len(saved))+'\ue001';saved[key]=match[0];return key
    masked=re.sub('|'.join(re.escape(v) for v in values),replace,text) if values else text
    return masked,saved,prefix


def _lines(runs):
    line=[]
    for run in runs:
        for part in run.text.splitlines(keepends=True):
            line.append(run.model_copy(update={'text':part}))
            if part.endswith(('\n','\r')):yield line;line=[]
    if line:yield line


def _bibliographic(line,known,prefix,listed=False):
    """Only URL/reference-only lines, not a line with an additional factual clause."""
    outside=[];references=0
    for run in line:
        if prefix in run.text:return False
        source=known.get(run.url)
        names={source.title,*source.titles,label(source)} if source else set()
        if source and re.search(r'\d(?:[.,]\d|\s*[×х*]\s*\d|\s*[A-Za-zА-Яа-я])',run.text):
            # A factual numeric anchor must not disappear merely because its
            # wording also happens to be the source title.
            if run.text.strip() not in {source.url,urlsplit(source.url).hostname}:return False
        if source and run.text.strip() in names|{source.url} and (listed or run.text.strip()==source.url):
            references+=1;continue
        text=run.text
        for match in reversed(list(URL.finditer(text))):
            url=_url_value(match[0],known)
            if url not in known:continue
            references+=1;source=known[url]
            text=text[:match.start()]+text[match.start()+len(url):]
            # Also recognize the existing "saved title — URL" bibliography shape.
            for name in sorted({source.title,*source.titles,label(source)},key=len,reverse=True):
                if text.strip().strip('—-:;,. \n')==name:text='';break
        outside.append(text)
    rest=''.join(outside).strip()
    rest=re.sub(r'^[-*•]|^\d+[.)]', '',rest).strip()
    if rest.rstrip(':').casefold() in HEADINGS:rest=''
    return bool(references) and not rest.strip(' \t\r\n,;:—-()[]')


def _body(text,sources,literals,providers):
    masked,protected,prefix=_protect(text,literals)
    # Remove only proven citation tokens, never an arbitrary annotated claim span.
    for source in sources:
        for citation in source.citations:
            if (0<=citation.start<citation.end<=len(text) and text[citation.start:citation.end]==citation.text
                    and REFERENCE.fullmatch(citation.text) and not any(citation.text in value for value in literals)):
                masked=masked.replace(citation.text,'')
    masked=OPAQUE.sub('',masked)
    lines=list(_lines(prose(masked)));known={source.url:source for source in sources}
    bibliography=[];listed=False
    for line in lines:
        value=''.join(run.text for run in line).strip()
        if value.rstrip(':').casefold() in HEADINGS:listed=True
        provider_line=re.sub(r'^[-*•]\s+|^\d+[.)]\s+','',value)
        provider_only=any(provider_line=='Сервис web_search: '+p for p in providers)
        is_reference=provider_only or _bibliographic(line,known,prefix,listed or bool(re.match(r'^[-*•]\s|^\d+[.)]\s',value)))
        bibliography.append(is_reference)
        if value and value.rstrip(':').casefold() not in HEADINGS and not is_reference:listed=False
    result=[]
    for index,line in enumerate(lines):
        text_line=''.join(run.text for run in line).strip()
        heading=text_line.rstrip(':').casefold() in HEADINGS
        following=index+1
        while following<len(lines) and not ''.join(r.text for r in lines[following]).strip():following+=1
        if bibliography[index] or heading and following<len(lines) and bibliography[following]:continue
        for run in line:
            # Anchor labels may contain actual facts. Keep them, remove the link;
            # all clickable citations are emitted exactly once in the compact block.
            value=URL.sub(lambda match:match[0][len(_url_value(match[0],known)):],run.text)
            if value:result.append(run.model_copy(update={'text':value,'url':None}))
    token=re.compile(re.escape(prefix)+r'\d+\ue001')
    restored=[]
    for run in result:
        offset=0
        for match in token.finditer(run.text):
            if match.start()>offset:restored.append(run.model_copy(update={'text':run.text[offset:match.start()]}))
            raw=protected[match[0]]
            # Literal URLs in source data remain text, not an additional citation.
            restored.append(PresentationRun(text=raw,marks=['code'] if URL.search(raw) else run.marks))
            offset=match.end()
        if offset<len(run.text):restored.append(run.model_copy(update={'text':run.text[offset:]}))
    # Trim only outer prose whitespace left by removed citation-only lines.
    while restored and not restored[0].text.strip():restored.pop(0)
    while restored and not restored[-1].text.strip():restored.pop()
    if restored:
        restored[0]=restored[0].model_copy(update={'text':restored[0].text.lstrip()})
        restored[-1]=restored[-1].model_copy(update={'text':restored[-1].text.rstrip()})
    return restored


def compact(text,sources,literals=(),providers=()):
    sources=merge_sources(sources)
    selected=select(sources,text)
    runs=_body(text,sources,literals,providers)
    if not ''.join(run.text for run in runs).strip():runs=literal('Поиск вернул ссылки без отдельного пояснения.')
    if selected:
        runs+=literal('\n\nИсточники: ')
        for n,source in enumerate(selected):
            if n:runs+=literal(', ')
            runs.append(PresentationRun(text=label(source),url=source.url))
    else:runs+=literal('\n\nПодтверждённые ссылки не предоставлены.')
    return sources,''.join(run.text for run in runs),runs


def full(saved):
    sources=merge_sources(saved['reply'].sources) if saved else []
    if not sources:return [],'Нет сохранённого списка подтверждённых ссылок для текущего контекста.',None
    runs=literal('Все источники последнего подходящего поиска:\n')
    for n,source in enumerate(sources):
        if n:runs+=literal('\n')
        runs.append(PresentationRun(text=label(source),url=source.url if safe_link(source.url) else None))
    return sources,''.join(run.text for run in runs),runs
