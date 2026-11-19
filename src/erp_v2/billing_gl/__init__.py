"""B-15 live billing -> GL mapper: an in-process consumer of ERP's own mod_billing accounting facts.

See docs/billing-gl-mapper.md. Every fact is posted (or dispositioned) exactly once through the posting bridge.
"""
