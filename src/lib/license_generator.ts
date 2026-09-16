import { createHmac, randomBytes } from 'crypto';

export type LicensePlan = 'STARTER' | 'PRO' | 'MAX';
export type LicenseDuration = '1M' | '1Y';

export const VALID_LICENSE_PLANS: LicensePlan[] = ['STARTER', 'PRO', 'MAX'];

// Seules ces deux durées peuvent être générées automatiquement via Stripe.
// Les licences permanentes restent réservées à une génération manuelle par l'admin.
export const LICENSE_DURATION_DAYS: Record<LicenseDuration, number> = {
  '1M': 30,
  '1Y': 365,
};

export function isValidLicensePlan(value: string): value is LicensePlan {
  return (VALID_LICENSE_PLANS as string[]).includes(value);
}

export function billingCycleToDuration(billingCycle: string): LicenseDuration {
  if (billingCycle === 'monthly') return '1M';
  if (billingCycle === 'yearly') return '1Y';
  throw new Error(`billing_cycle inconnu: "${billingCycle}" (attendu "monthly" ou "yearly")`);
}

export function computeExpirationDate(startDate: Date, duration: LicenseDuration): Date {
  const days = LICENSE_DURATION_DAYS[duration];
  const expiration = new Date(startDate.getTime());
  expiration.setUTCDate(expiration.getUTCDate() + days);
  return expiration;
}

function generateUniqueToken(): string {
  return randomBytes(2).toString('hex').toUpperCase();
}

function computeChecksum(payload: string, secret: string): string {
  return createHmac('sha256', secret).update(payload).digest('hex').slice(0, 4).toUpperCase();
}

export interface GenerateLicenseKeyParams {
  plan: LicensePlan;
  duration: LicenseDuration;
  secret: string;
}

export function generateLicenseKey({ plan, duration, secret }: GenerateLicenseKeyParams): string {
  const token = generateUniqueToken();
  const checksum = computeChecksum(`${plan}-${duration}-${token}`, secret);
  return `KODO-${plan}-${duration}-${token}-${checksum}`;
}

export function verifyLicenseKey(licenseKey: string, secret: string): boolean {
  const parts = licenseKey.split('-');
  if (parts.length !== 5 || parts[0] !== 'KODO') return false;
  const [, plan, duration, token, checksum] = parts;
  const expectedChecksum = computeChecksum(`${plan}-${duration}-${token}`, secret);
  return expectedChecksum === checksum;
}
