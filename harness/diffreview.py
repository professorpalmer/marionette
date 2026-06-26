"""Diff review parsing and reconstruction utilities."""

def extract_path(line: str, prefix: str) -> str:
    p = line[len(prefix):].strip()
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    elif p.startswith("\"a/") or p.startswith("\"b/"):
        p = p[3:]
    if p.endswith("\""):
        p = p[:-1]
    return p

def parse_unified_diff(diff_text: str) -> list:
    files = []
    current_file = None
    current_hunk = None
    
    lines = diff_text.splitlines(keepends=True)
    
    file_index = -1
    hunk_index = -1
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        if line.startswith("diff --git "):
            file_index += 1
            hunk_index = -1
            
            path = ""
            parts = line.split(" ")
            if len(parts) >= 4:
                b_part = parts[3]
                if b_part.startswith("b/") or b_part.startswith("\"b/"):
                    path = extract_path(b_part, "b/") if b_part.startswith("b/") else extract_path(b_part, "\"b/")
            
            current_file = {
                "path": path,
                "headers": [line],
                "hunks": []
            }
            files.append(current_file)
            current_hunk = None
            i += 1
            continue
            
        if current_file is not None and current_hunk is None:
            if line.startswith("@@ "):
                hunk_index += 1
                current_hunk = {
                    "id": f"{file_index}:{hunk_index}",
                    "header": line,
                    "lines": [],
                    "status": "pending"
                }
                current_file["hunks"].append(current_hunk)
            else:
                if line.startswith("+++ b/") or line.startswith("+++ \"b/"):
                    p = extract_path(line, "+++ ")
                    if p != "/dev/null":
                        current_file["path"] = p
                elif line.startswith("--- a/") or line.startswith("--- \"a/"):
                    p = extract_path(line, "--- ")
                    if p != "/dev/null" and not current_file["path"]:
                        current_file["path"] = p
                current_file["headers"].append(line)
            i += 1
            continue
            
        if current_file is not None and current_hunk is not None:
            if line.startswith("@@ "):
                hunk_index += 1
                current_hunk = {
                    "id": f"{file_index}:{hunk_index}",
                    "header": line,
                    "lines": [],
                    "status": "pending"
                }
                current_file["hunks"].append(current_hunk)
            elif line.startswith("diff --git "):
                current_hunk = None
                continue
            else:
                current_hunk["lines"].append(line)
            i += 1
            continue
            
        i += 1
        
    return files

def reconstruct_diff(files: list, decisions: dict) -> str:
    out_lines = []
    for f in files:
        accepted_hunks = [h for h in f["hunks"] if decisions.get(h["id"]) == "accept"]
        if not accepted_hunks:
            continue
        
        for h_line in f["headers"]:
            if not h_line.endswith("\n"):
                h_line += "\n"
            out_lines.append(h_line)
            
        for hunk in accepted_hunks:
            h_header = hunk["header"]
            if not h_header.endswith("\n"):
                h_header += "\n"
            out_lines.append(h_header)
            for hunk_line in hunk["lines"]:
                if not hunk_line.endswith("\n"):
                    hunk_line += "\n"
                out_lines.append(hunk_line)
                
    return "".join(out_lines)
