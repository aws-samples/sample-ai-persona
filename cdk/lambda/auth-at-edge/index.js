const { Authenticator } = require('cognito-at-edge');
const authenticator = new Authenticator({
  region: 'us-east-1',
  userPoolId: '',
  userPoolAppId: '',
  userPoolDomain: '',
  cookieExpirationDays: 1,
  httpOnly: true,
  sameSite: 'Lax',
});
exports.handler = async (request) => authenticator.handle(request);
