"""Show conflict blocks with explicit indentation counts (output-filter safe)."""
import sys

path = sys.argv[1]
maxlines = int(sys.argv[2]) if len(sys.argv) > 2 else 40
text = open(path, encoding="utf-8").read().splitlines()


def dump(lines, tag):
    for ln in lines:
        ind = len(ln) - len(ln.lstrip(" "))
        print(f"{tag}{ind:>3}|{ln.strip()}")


i = 0
idx = 0
while i < len(text):
    if text[i].startswith("<<<<<<<"):
        start = i
        mid = end = None
        j = i + 1
        while j < len(text):
            if text[j].startswith("=======") and mid is None:
                mid = j
            elif text[j].startswith(">>>>>>>"):
                end = j
                break
            j += 1
        idx += 1
        ours = text[start + 1:mid]
        theirs = text[mid + 1:end]
        print(f"##### CONFLICT #{idx} lines {start+1}-{end+1} ours={len(ours)}L theirs={len(theirs)}L")
        print("--OURS--")
        dump(ours[:maxlines], "o")
        if len(ours) > maxlines:
            print(f"o... +{len(ours)-maxlines} more")
        print("--THEIRS--")
        dump(theirs[:maxlines], "t")
        if len(theirs) > maxlines:
            print(f"t... +{len(theirs)-maxlines} more")
        i = end + 1
    else:
        i += 1
