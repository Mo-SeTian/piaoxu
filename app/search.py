import re
import unicodedata
from decimal import Decimal
from functools import lru_cache

from pypinyin import lazy_pinyin


@lru_cache(maxsize=8192)
def phonetic(value):
    # Keep syllable boundaries: “西安” must not match “先”.
    return '\x1f' + '\x1f'.join(lazy_pinyin(value)) + '\x1f'


def invoice_matcher(query):
    amounts, terms = [], []
    for term in unicodedata.normalize('NFKC', query).casefold().split():
        money = re.fullmatch(r'¥?(-?[0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)元?', term)
        cents = int(Decimal(money[1].replace(',', '')) * 100) if money else None
        # Unformatted digits also serve as filename or invoice number fragments.
        if money and not term.isdecimal():
            amounts.append(cents)
            continue
        sound = phonetic(term) if re.fullmatch(r'[\u3400-\u9fff]{2,}', term) else None
        terms.append((term, cents, sound))

    def matches(buyer, seller, filename, number, amount_cents):
        if any(amount != amount_cents for amount in amounts):
            return False
        if not terms:
            return True
        fields = [''.join(unicodedata.normalize('NFKC', value or '').casefold().split())
                  for value in (buyer, seller, filename, number)]
        for term, cents, sound in terms:
            if cents is not None and cents == amount_cents:
                continue
            if any(term in field for field in fields):
                continue
            if sound and any(sound in phonetic(field) for field in fields[:2]):
                continue
            return False
        return True

    return matches
