# AWS Resource Safety Checks

Apply only when CDK or infrastructure files are in the diff.

## DynamoDB

- Key schema changes cause table replacement (data loss)
- GSI additions/removals are safe but may take time
- Billing mode changes are safe

## ECS

- Task definition resource limit changes (CPU/memory) may cause rolling restart
- Container image changes trigger standard rolling deployment
- Task role changes take effect on next task launch

## CloudFront

- Behavior path pattern changes affect routing immediately
- Origin changes may cause brief 503 errors during propagation
- Cache invalidation needed for static asset changes

## Cognito

- User pool attribute changes (schema) cause pool replacement (user data loss)
- App client changes are safe
- Lambda trigger changes take effect immediately

## S3

- Bucket name changes cause replacement (data loss)
- Lifecycle rule changes are safe
- CORS configuration changes take effect immediately
