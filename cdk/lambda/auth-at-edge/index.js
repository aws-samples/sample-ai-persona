const { Authenticator } = require('cognito-at-edge');
const authenticator = new Authenticator({
  region: 'us-east-1',
  userPoolId: '',
  userPoolAppId: '',
  userPoolDomain: '',
  cookieExpirationDays: 30,
  cookiePath: '/',
  httpOnly: true,
  sameSite: 'Lax',
  logLevel: 'warn',
  logoutConfiguration: {
    logoutUri: '/signout',
    logoutRedirectUri: '/__cognito_logout__',
  },
});
exports.handler = async (event) => {
  const response = await authenticator.handle(event);
  if (response.status === '302' && response.headers?.location?.[0]?.value?.includes('/__cognito_logout__')) {
    const cfDomain = event.Records[0].cf.request.headers.host[0].value;
    response.headers.location[0].value = 'https://' + '' + '/logout'
      + '?client_id=' + ''
      + '&logout_uri=' + encodeURIComponent('https://' + cfDomain + '/');
  }
  return response;
};
