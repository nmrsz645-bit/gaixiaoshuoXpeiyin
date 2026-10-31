"""User-requested deterministic line deletion and 7000-character limit."""
from bisect import bisect_right
from dataclasses import dataclass
import hashlib
import re

REVISION = 'head7000-lines-v2-hash-headings'
CHARACTER_LIMIT = 7000
_CHAPTER = re.compile(r'^第[0-9零〇一二三四五六七八九十百千万两]+章')
_NUMBERED = re.compile(r'^[0-9]+[.．](?![0-9])')
_HEADING_PREFIX = re.compile(r'^#+\s*')


@dataclass(frozen=True)
class PreparedText:
    text: str
    actions: tuple[dict, ...]
    kept_spans: tuple[dict, ...]
    original_characters: int
    prepared_characters: int


def prepare_text(text):
    pieces, spans, actions = [], [], []
    source_position = prepared_position = 0
    for number,line in enumerate(text.splitlines(keepends=True),1):
        stripped = line.strip()
        heading = _HEADING_PREFIX.sub('', stripped)
        reason = ('single_digit' if len(heading)==1 and heading in '0123456789'
                  else 'chapter_heading' if _CHAPTER.match(heading)
                  else 'numbered_line' if _NUMBERED.match(heading) else '')
        if reason:
            actions.append(dict(kind='delete_line',stage='input',reason=reason,line=number,
                start=source_position,end=source_position+len(line),before_preview=line[:160],
                before_sha256=hashlib.sha256(line.encode('utf-8')).hexdigest(),after=''))
        else:
            pieces.append(line)
            spans.append(dict(input_start=source_position,input_end=source_position+len(line),
                              output_start=prepared_position,output_end=prepared_position+len(line)))
            prepared_position += len(line)
        source_position += len(line)
    filtered = ''.join(pieces)
    visible = 0
    cut = len(filtered)
    for index,char in enumerate(filtered):
        if not char.isspace():
            visible += 1
            if visible > CHARACTER_LIMIT:
                # End immediately after character 7000, preserving preceding
                # whitespace and line breaks without admitting a 7001st char.
                cut = last_character_end
                break
            last_character_end = index+1
    if cut < len(filtered):
        actions.append(dict(kind='truncate',stage='after_line_cleanup',start=cut,end=len(filtered),
            character_limit=CHARACTER_LIMIT,removed_characters=sum(not c.isspace() for c in filtered[cut:]),
            removed_sha256=hashlib.sha256(filtered[cut:].encode('utf-8')).hexdigest(),after=''))
    kept=[]
    for span in spans:
        if span['output_start'] >= cut:
            break
        end=min(span['output_end'],cut)
        kept.append(dict(span,input_end=span['input_start']+end-span['output_start'],output_end=end))
    final=filtered[:cut]
    return PreparedText(final,tuple(actions),tuple(kept),sum(not c.isspace() for c in text),
                        sum(not c.isspace() for c in final))


def remap_output_mapping(mapping, prepared):
    starts=[s['input_start'] for s in prepared.kept_spans]
    def position(offset):
        index=bisect_right(starts,offset)-1
        if index < 0:
            return 0
        span=prepared.kept_spans[index]
        return span['output_start']+min(offset-span['input_start'],span['input_end']-span['input_start'])
    updated=[]
    for row in mapping:
        start,end=position(row['output_start']),position(row['output_end'])
        lines=[] if start==end else list(range(prepared.text.count('\n',0,start)+1,
                                              prepared.text.count('\n',0,end-1)+2))
        updated.append(dict(row,output_start=start,output_end=end,output_lines=lines))
    return updated
