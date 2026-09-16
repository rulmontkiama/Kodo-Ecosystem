import { describe, it, expect } from 'vitest';
import {
  billingCycleToDuration,
  computeExpirationDate,
  generateLicenseKey,
  verifyLicenseKey,
} from './license_generator';

describe('computeExpirationDate', () => {
  it('ajoute exactement 30 jours pour une durée "1M"', () => {
    const start = new Date('2026-01-15T10:00:00.000Z');
    const expiration = computeExpirationDate(start, '1M');
    expect(expiration.toISOString()).toBe('2026-02-14T10:00:00.000Z');
    const diffDays = (expiration.getTime() - start.getTime()) / (1000 * 60 * 60 * 24);
    expect(diffDays).toBe(30);
  });

  it('ajoute exactement 365 jours pour une durée "1Y"', () => {
    const start = new Date('2026-01-15T10:00:00.000Z');
    const expiration = computeExpirationDate(start, '1Y');
    expect(expiration.toISOString()).toBe('2027-01-15T10:00:00.000Z');
    const diffDays = (expiration.getTime() - start.getTime()) / (1000 * 60 * 60 * 24);
    expect(diffDays).toBe(365);
  });

  it("gère correctement le passage d'une année bissextile (2028)", () => {
    const start = new Date('2028-01-01T00:00:00.000Z');
    const expiration = computeExpirationDate(start, '1Y');
    expect(expiration.toISOString()).toBe('2028-12-31T00:00:00.000Z');
  });
});

describe('billingCycleToDuration', () => {
  it('mappe "monthly" vers "1M"', () => {
    expect(billingCycleToDuration('monthly')).toBe('1M');
  });

  it('mappe "yearly" vers "1Y"', () => {
    expect(billingCycleToDuration('yearly')).toBe('1Y');
  });

  it('lève une erreur pour une valeur inconnue', () => {
    expect(() => billingCycleToDuration('weekly')).toThrow();
  });
});

describe('generateLicenseKey / verifyLicenseKey', () => {
  const secret = 'test-secret';

  it('génère une clé au format KODO-{PLAN}-{DURATION}-{TOKEN}-{CHECKSUM}', () => {
    const key = generateLicenseKey({ plan: 'PRO', duration: '1Y', secret });
    expect(key).toMatch(/^KODO-PRO-1Y-[0-9A-F]{4}-[0-9A-F]{4}$/);
  });

  it("valide une clé qu'elle vient de générer", () => {
    const key = generateLicenseKey({ plan: 'STARTER', duration: '1M', secret });
    expect(verifyLicenseKey(key, secret)).toBe(true);
  });

  it('rejette une clé altérée', () => {
    const key = generateLicenseKey({ plan: 'MAX', duration: '1Y', secret });
    const lastChar = key.slice(-1);
    const tampered = key.slice(0, -1) + (lastChar === '0' ? '1' : '0');
    expect(verifyLicenseKey(tampered, secret)).toBe(false);
  });

  it('rejette une clé validée avec le mauvais secret', () => {
    const key = generateLicenseKey({ plan: 'PRO', duration: '1M', secret });
    expect(verifyLicenseKey(key, 'wrong-secret')).toBe(false);
  });
});
