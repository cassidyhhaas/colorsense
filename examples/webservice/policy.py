"""The process-wide :class:`~colorsense.PolitenessPolicy` shared by every request.

One policy for the process: its render cache, per-host rate limiter, robots cache, and
render-concurrency semaphore are all per-policy state, so sharing the instance is what
makes them effective.

- identifiable UA: site operators can attribute and contact (SECURITY.md §3);
- robots respected (the default): kept explicit here because disabling it for
  user-supplied targets would be exactly the unaccountable choice SECURITY.md warns about;
- request_filter: the library-shipped egress gate over every browser request (§1).
  The navigation-host allowlist is enforced pre-call (validate_target_url); the egress
  filter deliberately gets no allowlist, since an allowed page legitimately loads
  sub-resources from other (public) hosts;
- max_concurrent_renders: the §2 concurrency cap, in-library.
"""

from __future__ import annotations

from colorsense import PolitenessPolicy, block_private_networks
from examples.webservice.settings import MAX_CONCURRENT_ANALYSES

POLICY = PolitenessPolicy(
    user_agent=(
        "colorsense-example-webservice/0.1 "
        "(+https://github.com/cassidyhhaas/colorsense/tree/main/examples)"
    ),
    respect_robots=True,
    min_interval=1.0,
    request_filter=block_private_networks(),
    max_concurrent_renders=MAX_CONCURRENT_ANALYSES,
)
