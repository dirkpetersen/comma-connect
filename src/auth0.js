// Auth0 login for the self-hosted deployment (see CLAUDE.md "Self-hosted AWS deployment").
//
// Replaces comma's OAuth seam only: Auth0 owns login (Google / GitHub / LinkedIn) and token
// acquisition; the resulting access token is dropped into my-comma-auth's storage so the rest
// of the app (Request.configure, isAuthenticated, logOut) is untouched.
//
// Config comes from config.js at runtime (like COMMA_URL_ROOT):
//   window.AUTH0_DOMAIN    e.g. 'dev-xyz123.us.auth0.com'   ('' = Auth0 disabled, legacy comma auth)
//   window.AUTH0_CLIENT_ID the SPA application's client ID
//   window.AUTH0_AUDIENCE  the Auth0 API identifier (makes access tokens verifiable RS256 JWTs)
import { createAuth0Client } from '@auth0/auth0-spa-js';
import window from 'global/window';

export const CALLBACK_PATH = '/auth0-callback';

// Auth0 connection names for the three social providers.
export const CONNECTIONS = {
  google: 'google-oauth2',
  github: 'github',
  linkedin: 'linkedin',
};

let clientPromise = null;

export function isConfigured() {
  return Boolean(window.AUTH0_DOMAIN && window.AUTH0_CLIENT_ID);
}

function getClient() {
  if (!clientPromise) {
    clientPromise = createAuth0Client({
      domain: window.AUTH0_DOMAIN,
      clientId: window.AUTH0_CLIENT_ID,
      cacheLocation: 'localstorage',
      useRefreshTokens: true,
      authorizationParams: {
        redirect_uri: `${window.location.origin}${CALLBACK_PATH}`,
        ...(window.AUTH0_AUDIENCE ? { audience: window.AUTH0_AUDIENCE } : {}),
      },
    });
  }
  return clientPromise;
}

// Called once at app start (before MyCommaAuth.init). Completes a redirect callback if we are
// on CALLBACK_PATH, then returns a fresh access token if a session exists, else null.
export async function init() {
  const auth0 = await getClient();

  if (window.location && window.location.pathname === CALLBACK_PATH) {
    const params = new URLSearchParams(window.location.search);
    if (params.has('code') && params.has('state')) {
      try {
        await auth0.handleRedirectCallback();
      } catch (err) {
        console.error('auth0 redirect callback failed', err);
      }
    }
    window.history.replaceState({}, document.title, '/');
  }

  try {
    if (await auth0.isAuthenticated()) {
      return await auth0.getTokenSilently();
    }
  } catch (err) {
    console.error('auth0 token fetch failed', err);
  }
  return null;
}

export async function login(connection) {
  const auth0 = await getClient();
  await auth0.loginWithRedirect({ authorizationParams: { connection } });
}

export async function logout() {
  const auth0 = await getClient();
  await auth0.logout({ logoutParams: { returnTo: window.location.origin } });
}
