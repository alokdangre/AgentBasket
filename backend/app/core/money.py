from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Money:
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if self.amount_minor < 0:
            raise ValueError("Money cannot be negative")
        if len(self.currency) != 3 or not self.currency.isalpha():
            raise ValueError("Currency must be a three-letter ISO 4217 code")

    def __add__(self, other: "Money") -> "Money":
        if self.currency.upper() != other.currency.upper():
            raise ValueError("Cannot add money in different currencies")
        return Money(self.amount_minor + other.amount_minor, self.currency.upper())
