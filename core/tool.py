

def _duree(secondes) -> str:
    secondes = int(secondes)
    if secondes < 60:
        return f"{secondes} s"
    return f"{secondes // 60} min {secondes % 60:02d} s"
