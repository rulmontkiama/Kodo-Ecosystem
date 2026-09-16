import { NextResponse } from 'next/server';
import Stripe from 'stripe';
import { adminDb } from '@/lib/firebase/admin';
import {
  billingCycleToDuration,
  computeExpirationDate,
  generateLicenseKey,
  isValidLicensePlan,
  LicensePlan,
} from '@/lib/license_generator';

export const runtime = 'nodejs';

async function sendWelcomeEmail(params: { email: string; licenseKey: string; plan: LicensePlan }) {
  const RESEND_API_KEY = process.env.RESEND_API_KEY;

  if (!RESEND_API_KEY) {
    console.warn("⚠️ RESEND_API_KEY manquant. Simulation d'envoi d'email de bienvenue.");
    return;
  }

  const response = await fetch('https://api.resend.com/emails', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${RESEND_API_KEY}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      from: process.env.LICENSE_EMAIL_FROM || 'Kōdo POS <licences@kodo-solutions.com>',
      to: params.email,
      subject: 'Votre licence Kōdo POS',
      html: `<p>Merci pour votre achat du plan <strong>${params.plan}</strong>.</p>
             <p>Votre clé de licence : <code>${params.licenseKey}</code></p>
             <p>Pour l'activer, lancez Kōdo POS et saisissez cette clé dans les paramètres de licence.</p>`,
    }),
  });

  if (!response.ok) {
    console.error('Erreur envoi email de bienvenue:', await response.text());
  }
}

export async function POST(request: Request) {
  const secretKey = process.env.STRIPE_SECRET_KEY;
  const webhookSecret = process.env.STRIPE_WEBHOOK_SECRET;
  const hmacSecret = process.env.LICENSE_HMAC_SECRET;

  if (!secretKey || !webhookSecret || !hmacSecret) {
    console.error(
      'Configuration manquante (STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, LICENSE_HMAC_SECRET).'
    );
    return NextResponse.json({ error: 'Configuration serveur incomplète' }, { status: 500 });
  }

  const stripe = new Stripe(secretKey);

  const body = await request.text();
  const signature = request.headers.get('stripe-signature');

  if (!signature) {
    return NextResponse.json({ error: 'Signature Stripe manquante' }, { status: 400 });
  }

  let event: Stripe.Event;
  try {
    event = stripe.webhooks.constructEvent(body, signature, webhookSecret);
  } catch (err) {
    console.error('Signature webhook Stripe invalide:', err);
    return NextResponse.json({ error: 'Signature invalide' }, { status: 400 });
  }

  if (event.type !== 'checkout.session.completed') {
    return NextResponse.json({ received: true });
  }

  const session = event.data.object as Stripe.Checkout.Session;
  const metadata = session.metadata || {};

  const plan = metadata.plan;
  const billingCycle = metadata.billing_cycle;
  const customerEmail =
    metadata.customer_email || session.customer_details?.email || session.customer_email || undefined;

  if (!plan || !isValidLicensePlan(plan)) {
    console.error(`Plan invalide ou manquant dans les métadonnées Stripe: "${plan}"`);
    return NextResponse.json({ error: 'Plan invalide' }, { status: 400 });
  }
  if (!billingCycle) {
    console.error('billing_cycle manquant dans les métadonnées Stripe.');
    return NextResponse.json({ error: 'billing_cycle manquant' }, { status: 400 });
  }
  if (!customerEmail) {
    console.error('Email client introuvable pour la session', session.id);
    return NextResponse.json({ error: 'Email client manquant' }, { status: 400 });
  }

  let duration;
  try {
    duration = billingCycleToDuration(billingCycle);
  } catch (err) {
    console.error(err);
    return NextResponse.json({ error: 'billing_cycle invalide' }, { status: 400 });
  }

  const createdAt = new Date();
  const expiresAt = computeExpirationDate(createdAt, duration);
  const licenseKey = generateLicenseKey({ plan, duration, secret: hmacSecret });

  const stripeSubscriptionId =
    typeof session.subscription === 'string'
      ? session.subscription
      : session.subscription?.id ?? null;

  await adminDb
    .collection('licenses')
    .doc(licenseKey)
    .set({
      license_key: licenseKey,
      plan,
      duration,
      status: 'active',
      created_at: createdAt.toISOString(),
      expires_at: expiresAt.toISOString(),
      hardware_id: null,
      customer_email: customerEmail,
      stripe_subscription_id: stripeSubscriptionId,
    });

  await sendWelcomeEmail({ email: customerEmail, licenseKey, plan });

  return NextResponse.json({ received: true, license_key: licenseKey });
}
