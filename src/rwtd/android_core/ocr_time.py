"""Strict countdown parsing. Invalid/ambiguous text stays UNKNOWN, not zero."""
import re
import unicodedata


def normalize_hms(text):
    """Normalize separators only when all three numeric fields are present."""
    text=unicodedata.normalize('NFKC',text).strip()
    match=re.fullmatch(r'(\d{1,7})\s*[:.\-]\s*(\d{1,2})\s*[:.\-]\s*(\d{1,2})',text)
    return ':'.join(match.groups()) if match else None


def parse_time(text, format='auto'):
    text=unicodedata.normalize('NFKC',text).strip().lower()
    text=re.sub(r'^(?:剩餘(?:時間)?|倒數|remaining|time left)\s*[:：]?\s*','',text)
    if format not in {'auto','ms','hms','hm','units'}:
        return None
    if format in {'auto','hms'}:
        text=normalize_hms(text) or text
    match=re.fullmatch(r'(\d{1,7})\s*:\s*(\d{1,2})(?:\s*:\s*(\d{1,2}))?',text)
    if match and format!='units':
        a,b,c=match.groups();a,b=int(a),int(b)
        if c is not None:
            c=int(c)
            if format not in {'auto','hms'} or b>=60 or c>=60:
                return None
            return a*3600+b*60+c
        if b>=60 or format=='hms':
            return None
        if format=='hm':
            return a*3600+b*60 if a<24 else None
        return a*60+b
    if format not in {'auto','units'}:
        return None
    pattern=r'(\d{1,7})\s*(小時|小时|時|时|hours?|hrs?|h|分鐘|分钟|分|minutes?|mins?|m|秒鐘|秒钟|秒|seconds?|secs?|s)'
    matches=list(re.finditer(pattern,text))
    if not matches or re.sub(pattern,'',text).strip():
        return None
    total=0;last=4
    for match in matches:
        amount=int(match[1]);unit=match[2]
        rank=3 if unit in {'小時','小时','時','时','hour','hours','hr','hrs','h'} else 2 if unit in {'分鐘','分钟','分','minute','minutes','min','mins','m'} else 1
        if rank>=last:
            return None
        total+=amount*{3:3600,2:60,1:1}[rank];last=rank
    return total
