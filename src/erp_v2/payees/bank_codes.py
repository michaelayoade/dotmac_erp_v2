"""Bank code list and NUBAN check-digit rule for the bulk payee command (B-11).

The code list is deliberately closed: a code outside it is refused as ``unknown_bank_code`` and Finance either
corrects the row or has the list extended in a reviewed change. Codes are the CBN institution codes: the
three-digit commercial-bank codes and the five/six-digit codes of the common mobile-money and microfinance
institutions. Finance must confirm the list against the bank letters before the first apply.

NUBAN (CBN standard): ten digits, the last is a check digit over ``bank code (3) + first nine digits``
with weights 3,7,3,3,7,3,3,7,3,3,7,3. The check is only defined for three-digit bank codes; the longer
institution codes are validated by format only.
"""

from __future__ import annotations

from typing import Final

BANK_NAMES: Final[dict[str, str]] = {
    "011": "First Bank of Nigeria",
    "023": "Citibank Nigeria",
    "030": "Heritage Bank",
    "032": "Union Bank of Nigeria",
    "033": "United Bank for Africa",
    "035": "Wema Bank",
    "044": "Access Bank",
    "050": "Ecobank Nigeria",
    "057": "Zenith Bank",
    "058": "Guaranty Trust Bank",
    "063": "Access Bank (Diamond)",
    "068": "Standard Chartered Bank Nigeria",
    "070": "Fidelity Bank",
    "076": "Polaris Bank",
    "082": "Keystone Bank",
    "100": "SunTrust Bank Nigeria",
    "101": "Providus Bank",
    "102": "Titan Trust Bank",
    "103": "Globus Bank",
    "214": "First City Monument Bank",
    "215": "Unity Bank",
    "221": "Stanbic IBTC Bank",
    "232": "Sterling Bank",
    "301": "Jaiz Bank",
    "50211": "Kuda Microfinance Bank",
    "50515": "Moniepoint Microfinance Bank",
    "999991": "PalmPay",
    "999992": "OPay",
}

_WEIGHTS: Final = (3, 7, 3, 3, 7, 3, 3, 7, 3, 3, 7, 3)


def nuban_check_digit(bank_code: str, serial9: str) -> int:
    total = sum(int(d) * w for d, w in zip(bank_code + serial9, _WEIGHTS, strict=True))
    return (10 - total % 10) % 10


def valid_nuban(bank_code: str, account_number: str) -> bool:
    if len(account_number) != 10 or not account_number.isascii() or not account_number.isdigit():
        return False
    if len(bank_code) != 3:
        return True  # check digit defined for three-digit codes only
    return nuban_check_digit(bank_code, account_number[:9]) == int(account_number[9])
