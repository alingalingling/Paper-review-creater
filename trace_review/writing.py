"""Writing constraints are independent from evidence retrieval and JSON layout."""
import re

_NUMBER = r"[0-9零〇一二两三四五六七八九十百千万]+"
_DIGITS = {char: value for value, char in enumerate("零一二三四五六七八九")}
_DIGITS.update({"〇": 0, "两": 2})


def number_value(value):
    if value.isascii() and value.isdigit():
        return int(value)
    if not any(char in value for char in "十百千万"):
        return int("".join(str(_DIGITS[char]) for char in value))
    total = section = digit = 0
    for char in value:
        if char in _DIGITS:
            digit = _DIGITS[char]
        elif char == "万":
            total += (section + digit or 1) * 10000
            section = digit = 0
        else:
            section += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            digit = 0
    return total + section + digit


def length_constraints(topic, requirements=""):
    # Requirements override a length mentioned in the topic when both are present.
    text = (requirements if re.search(rf"{_NUMBER}\s*(?:个)?字", requirements) else topic).replace("，", ",")
    result = {}
    patterns = [
        (rf"({_NUMBER})\s*(?:到|至|[-—~～])\s*({_NUMBER})\s*(?:个)?字", "range"),
        (rf"(?:不少于|不低于|不小于|至少|不要少于|不应少于|不要低于|不得少于|超过)\s*({_NUMBER})\s*(?:个)?字", "min"),
        (rf"(?:不超过|不得超过|不要超过|不多于|最多|至多|少于|小于|不大于|不要多于)\s*({_NUMBER})\s*(?:个)?字", "max"),
        (rf"({_NUMBER})\s*(?:个)?字\s*(?:以上|起|及以上)", "min"),
        (rf"({_NUMBER})\s*(?:个)?字\s*(?:以内|以下|及以下)", "max"),
        (rf"(?:约|大约|大概|控制在|写成|写到|正好|恰好)\s*({_NUMBER})\s*(?:个)?字", "target"),
        (rf"({_NUMBER})\s*(?:个)?字\s*左右", "target"),
    ]
    matches = []
    for pattern, kind in patterns:
        matches.extend((match.start(), match.end(), kind, match.groups()) for match in re.finditer(pattern, text))
    # Do not let the '超过' inside '不超过' create an opposite minimum.
    matches = [item for item in matches if not any(other[0] < item[0] and item[1] <= other[1] for other in matches)]
    for _, _, kind, values in sorted(matches):
        numbers = [number_value(value) for value in values]
        if kind == "range":
            result.update(min_characters=numbers[0], max_characters=numbers[1])
        elif kind == "target":
            result.update(min_characters=round(numbers[0]*.9), max_characters=round(numbers[0]*1.1), target_characters=numbers[0])
        else:
            result[("min" if kind == "min" else "max") + "_characters"] = numbers[0]
    if not matches:
        bare = re.search(rf"({_NUMBER})\s*(?:个)?字", text)
        if bare:
            target = number_value(bare.group(1))
            result.update(min_characters=round(target*.9), max_characters=round(target*1.1), target_characters=target)
    if result.get("min_characters", 0) > result.get("max_characters", float("inf")):
        raise ValueError("字数上下限有冲突，请调整写作要求。")
    return result


def character_count(claims):
    return sum(char.isalnum() for claim in claims for char in claim["text"])


def assess_writing(claims, constraints):
    count = character_count(claims)
    issues = []
    if count < constraints.get("min_characters", 0):
        issues.append(f"正文当前 {count} 字，要求至少 {constraints['min_characters']} 字。")
    if count > constraints.get("max_characters", float("inf")):
        issues.append(f"正文当前 {count} 字，要求不超过 {constraints['max_characters']} 字。")
    return {"character_count": count, "constraints": constraints, "met": not issues, "issues": issues}
