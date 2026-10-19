"""Small, source-aware presentation adapter for the existing Telegram outbox.

Only b/i/code/pre/a are emitted. Unknown or malformed syntax stays literal.
Facts are protected before interpreting prose; HTML is always rebuilt from text.
"""
from html import escape, unescape
from html.parser import HTMLParser
from itertools import groupby
import re
import unicodedata

from .contracts import PresentationRun
from .transport import safe_source


def safe_link(url):
    return (isinstance(url,str) and safe_source(url)
            and not any(ord(ch)<32 or ord(ch)==127 or ch in '\\<>"\'' for ch in url))


def _run(text,marks=(),url=None):
    return PresentationRun(text=text,marks=list(dict.fromkeys(marks)),url=url if safe_link(url) else None)


def _markdown(text,marks=(),url=None,depth=0,restore=lambda value:value):
    """Bounded syntax scanner, not a global substitution over technical data.

    Single stars/underscores, tables, lists and unmatched delimiters stay literal.
    Backslash escapes are interpreted only inside a recognized Markdown construct.
    """
    if depth>8:return [_run(text,marks,url)]
    result=[];plain=[];i=0
    def flush():
        if plain:result.append(_run(''.join(plain),marks,url));plain.clear()
    while i<len(text):
        if depth and text[i]=='\\' and i+1<len(text) and text[i+1] in r'\`*_{}[]()#+-.!|>~=':
            plain.append(text[i+1]);i+=2;continue
        # A heading is recognized only at a line boundary in model prose.
        if (i==0 or text[i-1]=='\n'):
            heading=re.match(r'#{1,6} ([^\n]+)',text[i:])
            if heading and heading[1].strip():
                flush();result.extend(_markdown(heading[1],(*marks,'b'),url,depth+1,restore))
                i+=len(heading[0]);continue
        boundary=i==0 or not (text[i-1].isalnum() or text[i-1] in '_\\')
        if boundary and text.startswith('**',i):
            end=text.find('**',i+2)
            if end>i+2 and text[i+2:end].strip()==text[i+2:end] and (end+2==len(text) or not text[end+2].isalnum()):
                flush();result.extend(_markdown(text[i+2:end],(*marks,'b'),url,depth+1,restore));i=end+2;continue
        if text[i]=='`' and boundary:
            marker='```' if text.startswith('```',i) else '`'
            end=text.find(marker,i+len(marker))
            if end>i+len(marker):
                flush();value=text[i+len(marker):end]
                if marker=='```' and '\n' in value:
                    first,rest=value.split('\n',1)
                    if re.fullmatch(r'[A-Za-z0-9_+-]*',first):value=rest
                result.append(_run(value,('pre' if marker=='```' else 'code',),url))
                i=end+len(marker);continue
        if text[i]=='[' and (i==0 or text[i-1] not in '!\\'):
            end=text.find('](',i+1)
            if end!=-1 and '\n' not in text[i:end]:
                cursor=end+2;balance=1
                while cursor<len(text) and balance:
                    if text[cursor]=='(':balance+=1
                    elif text[cursor]==')':balance-=1
                    if balance:cursor+=1
                if not balance:
                    flush();target=restore(text[end+2:cursor])
                    result.extend(_markdown(text[i+1:end],marks,target if safe_link(target) else None,depth+1,restore))
                    i=cursor+1;continue
        plain.append(text[i]);i+=1
    flush()
    return result


class _HTML(HTMLParser):
    """Parse a small balanced subset to data; never forward received tags/attrs."""
    aliases={'b':'b','strong':'b','i':'i','em':'i','code':'code','pre':'pre',
             **{f'h{n}':'b' for n in range(1,7)}}

    def __init__(self,restore):
        super().__init__(convert_charrefs=False)
        self.stack=[];self.runs=[];self.known=False;self.valid=True
        self.restore=restore

    def handle_starttag(self,tag,attrs):
        if tag not in {*self.aliases,'a'} or len(self.stack)>=8:
            self.valid=False;return
        self.known=True
        # Attributes are discarded except a single validated href on a link.
        hrefs=[self.restore(value) if value else value for name,value in attrs if name=='href']
        target=hrefs[0] if tag=='a' and len(hrefs)==1 and safe_link(hrefs[0]) else None
        if tag=='a' and any(t=='a' for t,_,_ in self.stack):self.valid=False
        if tag in {'code','pre'} and any(t not in {'pre'} for t,_,_ in self.stack):self.valid=False
        self.stack.append((tag,self.aliases.get(tag),target))

    def handle_endtag(self,tag):
        if not self.stack or self.stack[-1][0]!=tag:self.valid=False;return
        self.stack.pop()

    def handle_startendtag(self,tag,attrs):self.valid=False
    def handle_comment(self,data):self.valid=False
    def handle_decl(self,decl):self.valid=False
    def handle_pi(self,data):self.valid=False
    def unknown_decl(self,data):self.valid=False

    def handle_entityref(self,name):self._entity('&'+name+';')
    def handle_charref(self,name):self._entity('&#'+name+';')

    def _entity(self,value):
        # Decoded entity characters are data, not a fresh Markdown/HTML input.
        marks=[mark for _,mark,_ in self.stack if mark]
        url=next((target for _,_,target in reversed(self.stack) if target),None)
        self.runs.append(_run(unescape(value),marks,url))

    def handle_data(self,data):
        marks=[mark for _,mark,_ in self.stack if mark]
        url=next((target for _,_,target in reversed(self.stack) if target),None)
        if any(mark in {'code','pre'} for mark in marks):self.runs.append(_run(data,marks,url))
        else:self.runs.extend(_markdown(data,marks,url,restore=self.restore))


def _parse(text,restore):
    parser=_HTML(restore)
    try:parser.feed(text);parser.close()
    except (ValueError,AssertionError):return [_run(text)]
    if not parser.valid or parser.stack:return [_run(text)]
    return parser.runs if parser.known else _markdown(text,restore=restore)


def prose(text,literals=()):
    """Opaque source spans survive prose formatting, including their real markup.

    A placeholder is only private parser memory. It is restored before building
    the durable presentation; it can never become an HTML tag or URL by itself.
    """
    values=sorted({line for value in literals if value for line in value.splitlines() if line},key=lambda s:(-len(s),s))
    prefix='\ue000literal:'
    while prefix in text:prefix+=':'
    protected={};masked=text
    # Longest first: do not replace inside an already opaque literal token.
    if values:
        pattern=re.compile('|'.join(re.escape(value) for value in values))
        def hide(match):
            token=prefix+str(len(protected))+'\ue001';protected[token]=match[0];return token
        masked=pattern.sub(hide,text)
    token_pattern=re.compile(re.escape(prefix)+r'\d+\ue001')
    def restore(value):return token_pattern.sub(lambda match:protected.get(match[0],match[0]),value)
    runs=_parse(masked,restore)
    result=[_run(restore(run.text),run.marks,restore(run.url) if run.url else None) for run in runs]
    if not ''.join(run.text for run in result) and text:return [_run(text)]
    # Entity decoding may create several adjacent data runs. Coalesce without
    # parsing again, avoiding one formatting entity per decoded character.
    return [_run(''.join(run.text for run in group),marks,url)
            for (marks,url),group in groupby(result,key=lambda run:(tuple(run.marks),run.url))]


def literal(text):return [_run(text)]


def _clusters(text):
    """Keep code points and common combining/ZWJ/flag emoji clusters together."""
    cluster='';regional=0
    for ch in text:
        code=ord(ch);is_regional=0x1F1E6<=code<=0x1F1FF
        attached=(unicodedata.category(ch) in {'Mn','Mc','Me'} or ch=='\u200d'
                  or cluster.endswith('\u200d') or 0x1F3FB<=code<=0x1F3FF
                  or 0xE0020<=code<=0xE007F or cluster=='\r' and ch=='\n'
                  or is_regional and regional==1)
        if cluster and not attached:yield cluster;cluster='';regional=0
        cluster+=ch;regional=regional+1 if is_regional else 0
    if cluster:yield cluster


def _size(text):return sum(2 if ord(ch)>0xffff else 1 for ch in text)


def html_chunks(runs,limit=3200):
    """Split visible text before escaping, reopening marks/links in each part."""
    if type(limit) is not int or not 2<=limit<=4096:raise ValueError('invalid_telegram_text_limit')
    visible=''.join(run.text for run in runs)
    cuts=[];start=0;position=0;size=0;line_end=None
    for cluster in _clusters(visible):
        # A pathological combining sequence longer than one message must still
        # retain every code point; no surrogate/code point is ever cut in half.
        atoms=[cluster] if _size(cluster)<=limit else list(cluster)
        for atom in atoms:
            units=_size(atom)
            while size+units>limit:
                end=line_end if line_end is not None and line_end>start else position
                cuts.append(end);start=end
                size=_size(visible[start:position]);line_end=None
            position+=len(atom)
            size+=units
            if atom.endswith('\n'):line_end=position
    if visible:cuts.append(len(visible))
    result=[];start=0;run_start=0;index=0
    for end in cuts:
        parts=[];cursor=start
        while index<len(runs) and run_start+len(runs[index].text)<=cursor:
            run_start+=len(runs[index].text);index+=1
        while cursor<end:
            run=runs[index];stop=min(end,run_start+len(run.text))
            text=escape(run.text[cursor-run_start:stop-run_start],quote=False)
            marks=list(dict.fromkeys(run.marks))
            # pre/code cannot enclose links or other formatting in this subset.
            if 'pre' in marks:marks=['pre']
            elif 'code' in marks:marks=['code']
            for mark in reversed(marks):text=f'<{mark}>'+text+f'</{mark}>'
            if run.url and safe_link(run.url) and not any(m in {'code','pre'} for m in marks):
                text='<a href="'+escape(run.url,quote=True)+'">'+text+'</a>'
            parts.append(text);cursor=stop
            if cursor==run_start+len(run.text):run_start=cursor;index+=1
        result.append(''.join(parts));start=end
    return result


def payloads(reply,user,*,limit=3200):
    runs=reply.presentation if reply.presentation is not None else literal(reply.text)
    return [dict(chat_id=user,text=part,parse_mode='HTML') for part in html_chunks(runs,limit)]
