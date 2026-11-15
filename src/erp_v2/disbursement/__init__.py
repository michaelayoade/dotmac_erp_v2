"""Shared disbursement bank-file library (B3 supplier payments; reused by B5 payroll payouts).

Nothing here talks to a bank or provider. A file is built in memory, handed to the caller's HTTP
response and forgotten: its bytes are never logged or stored, only a sha256 + row-count receipt.
"""
