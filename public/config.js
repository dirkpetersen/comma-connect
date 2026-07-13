window.COMMA_URL_ROOT = 'https://api.comma.ai/';
window.ATHENA_URL_ROOT = 'https://athena.comma.ai/';
window.BILLING_URL_ROOT = 'https://billing.comma.ai/';
window.USERADMIN_URL_ROOT = 'https://useradmin.comma.ai/';
// Self-hosted Auth0 login (Google/GitHub/LinkedIn) — empty = disabled (legacy comma auth).
// Fill from the Auth0 tenant (see CLAUDE.md "Auth0 authentication"): domain, SPA client ID,
// and the Auth0 API identifier used as the JWT audience.
window.AUTH0_DOMAIN = 'dev-cllrby28qiip47i0.us.auth0.com';
window.AUTH0_CLIENT_ID = 'OpUK79iwwOfkv1D9ERK0dHClZbkuspst';
window.AUTH0_AUDIENCE = 'https://connect-api.internetchen.de';   // Auth0 API identifier → verifiable RS256 access tokens
