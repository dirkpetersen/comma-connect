/* global AppleID */
import React, { Component } from 'react';
import { connect } from 'react-redux';
import Obstruction from 'obstruction';
import window from 'global/window';
import PropTypes from 'prop-types';
import qs from 'query-string';

import { withStyles } from '@material-ui/core/styles';
import Typography from '@material-ui/core/Typography';

import {config as AuthConfig, storage as AuthStorage} from '@commaai/my-comma-auth';

import * as Auth0 from '../auth0';

import Colors from '../colors';
import { AuthAppleIcon, AuthGithubIcon, AuthGoogleIcon, RightArrow } from '../icons';

import PWAIcon from './PWAIcon';

const styles = () => ({
  baseContainer: {
    width: '100%',
    height: '100vh',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
  },
  base: {
    overflowY: 'auto',
    padding: 20,
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    width: '100%',
  },
  logoImg: {
    height: 45,
    width: 'auto',
  },
  logoContainer: {
    width: 84,
    height: 84,
    backgroundColor: Colors.grey900,
    borderRadius: 17,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    flexShrink: 0,
  },
  logoSpacer: {
    height: 60,
    flexShrink: 2,
  },
  logoText: {
    fontSize: 36,
    fontWeight: 800,
    textAlign: 'center',
  },
  tagline: {
    width: 380,
    maxWidth: '90%',
    textAlign: 'center',
    margin: '10px 0 30px',
    fontSize: '18px',
  },
  logInButton: {
    cursor: 'pointer',
    alignItems: 'center',
    background: '#ffffff',
    display: 'flex',
    borderRadius: 80,
    fontSize: 21,
    height: 80,
    justifyContent: 'center',
    textDecoration: 'none',
    width: 400,
    maxWidth: '90%',
    marginBottom: 10,
    '&:hover': {
      background: '#eee',
    },
  },
  buttonText: {
    fontSize: 18,
    width: 190,
    textAlign: 'center',
    color: 'black',
    fontWeight: 600,
  },
  buttonImage: {
    height: 40,
  },
});

class AnonymousLanding extends Component {
  UNSAFE_componentWillMount() {
    if (typeof window.sessionStorage !== 'undefined' && sessionStorage.getItem('redirectURL') === null) {
      const { pathname } = this.props;
      sessionStorage.setItem('redirectURL', pathname);
    }
  }

  componentDidMount() {
    const q = new URLSearchParams(window.location.search);
    if (q.has('r')) {
      sessionStorage.setItem('redirectURL', q.get('r'));
    }

    if (Auth0.isConfigured()) {
      return;   // Auth0 owns login — don't load Apple's sign-in script (legacy comma auth only)
    }

    const script = document.createElement('script');
    document.body.appendChild(script);
    script.onload = () => {
      AppleID.auth.init({
        clientId: AuthConfig.APPLE_CLIENT_ID,
        scope: AuthConfig.APPLE_SCOPES,
        redirectURI: AuthConfig.APPLE_REDIRECT_URI,
        state: AuthConfig.APPLE_STATE,
      });
    };
    script.src = 'https://appleid.cdn-apple.com/appleauth/static/jsapi/appleid/1/en_US/appleid.auth.js';
    script.async = true;
    document.addEventListener('AppleIDSignInOnSuccess', (data) => {
      const { code, state } = data.detail.authorization;
      window.location = [AuthConfig.APPLE_REDIRECT_PATH, qs.stringify({ code, state })].join('?');
    });
    document.addEventListener('AppleIDSignInOnFailure', console.warn);
  }

  render() {
    const { classes } = this.props;

    const loginAsDemoUser = function() {
      AuthStorage.setCommaAccessToken('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjEwMzg5NTgwNzM1LCJuYmYiOjE3NDk1ODA3MzUsImlhdCI6MTc0OTU4MDczNSwiaWRlbnRpdHkiOiIwZGVjZGRjZmRmMjQxYTYwIn0.KsDzqJxgkYhAs4tCgrMJIdORyxO0CQNb0gHXIf8aUT0');
      window.location = window.location.origin;
    };

    return (
      <div className={classes.baseContainer}>
        <div className={classes.base}>
          <div className={classes.logoContainer}>
            <img alt="comma" src="/images/comma-white.png" className={classes.logoImg} />
          </div>
          <div className={classes.logoSpacer}>&nbsp;</div>
          <Typography className={classes.logoText}>comma connect</Typography>
          <Typography className={classes.tagline}>
            Manage your comma device, view your drives, and use comma prime features
          </Typography>
          {Auth0.isConfigured() ? (
            <>
              <a onClick={() => Auth0.login(Auth0.CONNECTIONS.google)} className={classes.logInButton}>
                <img className={classes.buttonImage} src={AuthGoogleIcon} alt="" />
                <Typography className={classes.buttonText}>Sign in with Google</Typography>
              </a>
              <a onClick={() => Auth0.login(Auth0.CONNECTIONS.github)} className={`${classes.logInButton} githubAuth`}>
                <img className={classes.buttonImage} src={AuthGithubIcon} alt="" />
                <Typography className={classes.buttonText}>Sign in with GitHub</Typography>
              </a>
              <a onClick={() => Auth0.login(Auth0.CONNECTIONS.linkedin)} className={classes.logInButton}>
                <svg className={classes.buttonImage} viewBox="0 0 24 24" fill="#0A66C2" xmlns="http://www.w3.org/2000/svg">
                  <path d="M20.45 20.45h-3.55v-5.57c0-1.33-.03-3.04-1.85-3.04-1.85 0-2.14 1.45-2.14 2.94v5.67H9.36V9h3.41v1.56h.05c.47-.9 1.63-1.85 3.36-1.85 3.6 0 4.27 2.37 4.27 5.46v6.28zM5.34 7.43a2.06 2.06 0 1 1 0-4.12 2.06 2.06 0 0 1 0 4.12zM7.12 20.45H3.56V9h3.56v11.45zM22.22 0H1.77C.79 0 0 .77 0 1.72v20.55C0 23.23.79 24 1.77 24h20.45c.98 0 1.78-.77 1.78-1.73V1.72C24 .77 23.2 0 22.22 0z" />
                </svg>
                <Typography className={classes.buttonText}>Sign in with LinkedIn</Typography>
              </a>
            </>
          ) : (
            <>
              <a href={AuthConfig.GOOGLE_REDIRECT_LINK} className={classes.logInButton}>
                <img className={classes.buttonImage} src={AuthGoogleIcon} alt="" />
                <Typography className={classes.buttonText}>Sign in with Google</Typography>
              </a>
              <a onClick={() => AppleID.auth.signIn()} className={classes.logInButton}>
                <img className={classes.buttonImage} src={AuthAppleIcon} alt="" />
                <Typography className={classes.buttonText}>Sign in with Apple</Typography>
              </a>
              <a href={AuthConfig.GITHUB_REDIRECT_LINK} className={`${classes.logInButton} githubAuth`}>
                <img className={classes.buttonImage} src={AuthGithubIcon} alt="" />
                <Typography className={classes.buttonText}>Sign in with GitHub</Typography>
              </a>
            </>
          )}

          <span className="max-w-sm text-center mt-2 mb-8 text-sm">
            Make sure to sign in with the same account if you have previously
            paired your comma device.
          </span>

          <a onClick={loginAsDemoUser}
            className="flex items-center pl-4 pr-3 py-2 font-medium border border-white rounded-full hover:bg-[rgba(255,255,255,0.1)] active:bg-[rgba(255,255,255,0.2)] transition-colors"
            style={{ height: 0, overflow: 'hidden', opacity: 0 }}
          >
            Try the demo
            <RightArrow className="ml-1 h-4" />
          </a>
        </div>
        <PWAIcon immediate />
      </div>
    );
  }
}

AnonymousLanding.propTypes = {
  pathname: PropTypes.string.isRequired,
  classes: PropTypes.object.isRequired,
};

const stateToProps = Obstruction({
  pathname: 'router.location.pathname',
});

export default connect(stateToProps)(withStyles(styles)(AnonymousLanding));
