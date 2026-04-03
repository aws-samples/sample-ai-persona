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
});
exports.handler = async (request) => authenticator.handle(request);
