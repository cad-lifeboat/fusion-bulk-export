"""Counts in words: "1 design", "8 designs", never "design(s)". Standard library only (the
command-line scripts use it too)."""

# Plurals that are not the word plus "s".
IRREGULAR = {'entry': 'entries', 'library': 'libraries', 'copy': 'copies', 'body': 'bodies', 'assembly': 'assemblies', 'is': 'are', 'has': 'have', 'was': 'were', 'this': 'these',
             'it': 'them', 'its': 'their', 'uses': 'use', 'holds': 'hold', 'takes': 'take', 'needs': 'need'}


def form(count, word, many=None):
    """word for one, its plural for any other count: many, else IRREGULAR, else word + "s".
    In a phrase ("whole project", "STEP file") the last word changes."""
    if count == 1:
        return word
    if many is not None:
        return many
    head, _, last = word.rpartition(' ')
    last = IRREGULAR.get(last, last + 's')
    return head + ' ' + last if head else last


def plural(count, word, many=None):
    """"1 design", "8 designs", "0 designs" (see form())."""
    return '%d %s' % (count, form(count, word, many))
