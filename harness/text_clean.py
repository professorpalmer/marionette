import re

COMMON_PROSE_WORDS = {
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "i", "it", "for", "not", "on", "with",
    "he", "as", "you", "do", "at", "this", "but", "his", "by", "from", "they", "we", "say", "her",
    "she", "or", "an", "will", "my", "one", "all", "would", "there", "their", "what", "so", "up",
    "out", "if", "about", "who", "get", "which", "go", "me", "is", "was", "are", "were", "been",
    "has", "had", "does", "did", "can", "could", "should", "your", "our"
}

def has_common_prose_word(line: str) -> bool:
    # Find all word characters
    words = re.findall(r'\b[a-zA-Z]+\b', line.lower())
    for w in words:
        if w in COMMON_PROSE_WORDS:
            return True
    return False

def is_raw_output_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    # If the line is too long, it's probably not a raw file listing / short output
    if len(line) > 80:
        return False
    # If it is a markdown header, ignore
    if s.startswith("#"):
        return False
    # If it contains common prose words, it is likely prose
    if has_common_prose_word(line):
        return False
    # Check for ls -l pattern: e.g. drwxrwxr-x or -rw-r--r--
    if re.match(r'^[d-][rwx-]{9}', s):
        return True
    # Otherwise, check if it contains path-like or file-like tokens
    tokens = s.split()
    if len(tokens) >= 1:
        is_file_tokens = True
        for token in tokens:
            # allow word characters, dots, slashes, dashes, underscores, colons, asterisks
            if not re.match(r'^[a-zA-Z0-9_\-\.\/:*]+$', token):
                is_file_tokens = False
                break
        if is_file_tokens and len(tokens) <= 5:
            return True
    return False

def is_pollution_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if s.startswith("USER: (") or "completed with exit code" in s:
        return True
    if re.match(r"^\s*Traceback\s*\(most\s+recent\s+call\s+last\):", s):
        return True
    if s.startswith("File \"") and ", line " in s:
        return True
    if re.match(r"^[A-Za-z0-9_]+Error:", s):
        return True
    if "During handling of the above exception" in s or "The above exception was the direct cause" in s:
        return True
    return False

def get_first_sentence(text: str) -> str:
    for line in text.splitlines():
        if is_pollution_line(line):
            continue
        s = line.strip()
        if s:
            # Find sentence boundaries: '.', '?', '!' followed by space or end of line
            match = re.split(r'(?<=[.!?])\s+', s)
            if match and match[0]:
                return match[0]
            return s
    return "Working..."

def clean_say(text: str) -> str:
    if not text:
        return ""
    
    lines = text.splitlines()
    cleaned_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped_line = line.strip()
        
        # 1. Remove tool-echo patterns
        if stripped_line.startswith("USER: (") or "completed with exit code" in stripped_line:
            i += 1
            while i < len(lines) and lines[i].strip() == "":
                i += 1
            continue
            
        # 2. Remove chained exception headers
        if "During handling of the above exception" in line or "The above exception was the direct cause" in line:
            i += 1
            while i < len(lines) and lines[i].strip() == "":
                i += 1
            continue
            
        # 3. Remove Python traceback blocks
        if re.match(r"^\s*Traceback\s*\(most\s+recent\s+call\s+last\):", line):
            i += 1
            while i < len(lines):
                curr_line = lines[i]
                if curr_line.strip() == "":
                    i += 1
                    continue
                if curr_line.startswith(" ") or curr_line.startswith("\t"):
                    i += 1
                    continue
                if "During handling of the above exception" in curr_line or "The above exception was the direct cause" in curr_line:
                    i += 1
                    continue
                # This should be the exception line (e.g. ValueError: ...)
                i += 1
                break
            
            # Now skip any subsequent blank lines to avoid leaving orphan empty lines
            while i < len(lines) and lines[i].strip() == "":
                i += 1
            continue
            
        cleaned_lines.append(line)
        i += 1
        
    # 4. Collapse runs of many (e.g. 6+) consecutive short lines that look like raw output
    final_lines = []
    run = []
    in_code_block = False
    
    for line in cleaned_lines:
        is_code_boundary = line.strip().startswith("```")
        if is_code_boundary:
            in_code_block = not in_code_block
            
        is_raw = False
        if not in_code_block and not is_code_boundary:
            is_raw = is_raw_output_line(line)
            
        if is_raw:
            run.append(line)
        else:
            if len(run) >= 6:
                final_lines.append("(output collapsed)")
            else:
                final_lines.extend(run)
            run = []
            final_lines.append(line)
            
    if len(run) >= 6:
        final_lines.append("(output collapsed)")
    else:
        final_lines.extend(run)
        
    # Reconstruct text
    result = "\n".join(final_lines).strip()
    
    # 5. Trim leading/trailing whitespace and collapse 3+ blank lines to 1
    # Note: 3+ blank lines means 4+ consecutive newlines, we collapse to 2 newlines (1 blank line)
    result = re.sub(r'\n{3,}', '\n\n', result)
    
    # 6. Fallback if empty or near-empty
    if len(result.strip()) < 5:
        fallback = get_first_sentence(text)
        return fallback if len(fallback.strip()) >= 5 else "Working..."
        
    return result
