"""ERP web surface: first browser slice over kernel identity.

Login consumes kernel identity storage directly (Party by tenant +
normalized email, UserCredential, AuthSession) exactly as the Starter
reference assembly does — ERP owns no credential or session storage.
"""

from erp_v2.web.routes import erp_web_feature, router

__all__ = ["erp_web_feature", "router"]
