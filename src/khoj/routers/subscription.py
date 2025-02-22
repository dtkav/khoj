# Standard Packages
from datetime import datetime, timezone
import logging
import os

# External Packages
from asgiref.sync import sync_to_async
from fastapi import APIRouter, Request
from starlette.authentication import requires
import stripe

# Internal Packages
from database import adapters


# Stripe integration for Khoj Cloud Subscription
stripe.api_key = os.getenv("STRIPE_API_KEY")
endpoint_secret = os.getenv("STRIPE_SIGNING_SECRET")
logger = logging.getLogger(__name__)
subscription_router = APIRouter()


@subscription_router.post("")
async def subscribe(request: Request):
    """Webhook for Stripe to send subscription events to Khoj Cloud"""
    event = None
    try:
        payload = await request.body()
        sig_header = request.headers["stripe-signature"]
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except ValueError as e:
        # Invalid payload
        raise e
    except stripe.error.SignatureVerificationError as e:
        # Invalid signature
        raise e

    event_type = event["type"]
    if event_type not in {
        "invoice.paid",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }:
        logger.warn(f"Unhandled Stripe event type: {event['type']}")
        return {"success": False}

    # Retrieve the customer's details
    subscription = event["data"]["object"]
    customer_id = subscription["customer"]
    customer = stripe.Customer.retrieve(customer_id)
    customer_email = customer["email"]

    # Handle valid stripe webhook events
    success = True
    if event_type in {"invoice.paid"}:
        # Mark the user as subscribed and update the next renewal date on payment
        subscription = stripe.Subscription.list(customer=customer_id).data[0]
        renewal_date = datetime.fromtimestamp(subscription["current_period_end"], tz=timezone.utc)
        user = await adapters.set_user_subscription(customer_email, is_recurring=True, renewal_date=renewal_date)
        success = user is not None
    elif event_type in {"customer.subscription.updated"}:
        user_subscription = await sync_to_async(adapters.get_user_subscription)(customer_email)
        # Allow updating subscription status if paid user
        if user_subscription and user_subscription.renewal_date:
            # Mark user as unsubscribed or resubscribed
            is_recurring = not subscription["cancel_at_period_end"]
            updated_user = await adapters.set_user_subscription(customer_email, is_recurring=is_recurring)
            success = updated_user is not None
    elif event_type in {"customer.subscription.deleted"}:
        # Reset the user to trial state
        user = await adapters.set_user_subscription(
            customer_email, is_recurring=False, renewal_date=False, type="trial"
        )
        success = user is not None

    logger.info(f'Stripe subscription {event["type"]} for {customer["email"]}')
    return {"success": success}


@subscription_router.patch("")
@requires(["authenticated"])
async def update_subscription(request: Request, email: str, operation: str):
    # Retrieve the customer's details
    customers = stripe.Customer.list(email=email).auto_paging_iter()
    customer = next(customers, None)
    if customer is None:
        return {"success": False, "message": "Customer not found"}

    if operation == "cancel":
        customer_id = customer.id
        for subscription in stripe.Subscription.list(customer=customer_id):
            stripe.Subscription.modify(subscription.id, cancel_at_period_end=True)
        return {"success": True}

    elif operation == "resubscribe":
        subscriptions = stripe.Subscription.list(customer=customer.id).auto_paging_iter()
        # Find the subscription that is set to cancel at the end of the period
        for subscription in subscriptions:
            if subscription.cancel_at_period_end:
                # Update the subscription to not cancel at the end of the period
                stripe.Subscription.modify(subscription.id, cancel_at_period_end=False)
                return {"success": True}
        return {"success": False, "message": "No subscription found that is set to cancel"}

    return {"success": False, "message": "Invalid operation"}
