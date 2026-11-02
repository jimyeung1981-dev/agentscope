# -*- coding: utf-8 -*-
"""Public resource access policy extension points.

Import from this package when customizing cross-owner resource access:

.. code-block:: python

    from agentscope.app.access import (
        ResourceAccessPolicyBase,
        ResourceKind,
        ResourcePermission,
        ResourceRef,
    )
"""

from ._policy import (
    DenyAllResourceAccessPolicy,
    ResourceAccessPolicyBase,
    ResourceKind,
    ResourcePermission,
    ResourceRef,
)
from ._tenant import (
    TenantIdentity,
    get_current_tenant,
    set_current_tenant,
)

__all__ = [
    "DenyAllResourceAccessPolicy",
    "ResourceAccessPolicyBase",
    "ResourceKind",
    "ResourcePermission",
    "ResourceRef",
    "TenantIdentity",
    "get_current_tenant",
    "set_current_tenant",
]
