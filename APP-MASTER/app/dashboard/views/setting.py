from io import BytesIO

import arrow
from flask import (
    render_template,
    request,
    redirect,
    url_for,
    flash,
)
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from flask_wtf.file import FileField
from wtforms import StringField, validators
from wtforms.fields.html5 import EmailField

from app import s3, email_utils
from app.config import (
    URL,
    FIRST_ALIAS_DOMAIN,
    ALIAS_RANDOM_SUFFIX_LENGTH,
)
from app.dashboard.base import dashboard_bp
from app.db import Session
from app.email_utils import (
    email_can_be_used_as_mailbox,
    personal_email_already_used,
)
from app.log import LOG
from app.models import (
    BlockBehaviourEnum,
    PlanEnum,
    File,
    ResetPasswordCode,
    EmailChange,
    User,
    Alias,
    CustomDomain,
    AliasGeneratorEnum,
    AliasSuffixEnum,
    ManualSubscription,
    SenderFormatEnum,
    SLDomain,
    CoinbaseSubscription,
    AppleSubscription,
)
from app.utils import random_string, sanitize_email


class SettingForm(FlaskForm):
    name = StringField("Name")
    profile_picture = FileField("Profile Picture")


class ChangeEmailForm(FlaskForm):
    email = EmailField(
        "email", validators=[validators.DataRequired(), validators.Email()]
    )


class PromoCodeForm(FlaskForm):
    code = StringField("Name", validators=[validators.DataRequired()])


@dashboard_bp.route("/setting", methods=["GET", "POST"])
@login_required
def setting():
    form = SettingForm()
    promo_form = PromoCodeForm()
    change_email_form = ChangeEmailForm()

    email_change = EmailChange.get_by(user_id=current_user.id)
    if email_change:
        pending_email = email_change.new_email
    else:
        pending_email = None

    if request.method == "POST":
        if request.form.get("form-name") == "update-email":
            if change_email_form.validate():
                # whether user can proceed with the email update
                new_email_valid = True
                if (
                    sanitize_email(change_email_form.email.data) != current_user.email
                    and not pending_email
                ):
                    new_email = sanitize_email(change_email_form.email.data)

                    # check if this email is not already used
                    if personal_email_already_used(new_email) or Alias.get_by(
                        email=new_email
                    ):
                        flash(f"Email {new_email} already used", "error")
                        new_email_valid = False
                    elif not email_can_be_used_as_mailbox(new_email):
                        flash(
                            "You cannot use this email address as your personal inbox.",
                            "error",
                        )
                        new_email_valid = False
                    # a pending email change with the same email exists from another user
                    elif EmailChange.get_by(new_email=new_email):
                        other_email_change: EmailChange = EmailChange.get_by(
                            new_email=new_email
                        )
                        LOG.w(
                            "Another user has a pending %s with the same email address. Current user:%s",
                            other_email_change,
                            current_user,
                        )

                        if other_email_change.is_expired():
                            LOG.d(
                                "delete the expired email change %s", other_email_change
                            )
                            EmailChange.delete(other_email_change.id)
                            Session.commit()
                        else:
                            flash(
                                "You cannot use this email address as your personal inbox.",
                                "error",
                            )
                            new_email_valid = False

                    if new_email_valid:
                        email_change = EmailChange.create(
                            user_id=current_user.id,
                            code=random_string(
                                60
                            ),  # todo: make sure the code is unique
                            new_email=new_email,
                        )
                        Session.commit()
                        send_change_email_confirmation(current_user, email_change)
                        flash(
                            "A confirmation email is on the way, please check your inbox",
                            "success",
                        )
                        return redirect(url_for("dashboard.setting"))
        if request.form.get("form-name") == "update-profile":
            if form.validate():
                profile_updated = False
                # update user info
                if form.name.data != current_user.name:
                    current_user.name = form.name.data
                    Session.commit()
                    profile_updated = True

                if form.profile_picture.data:
                    file_path = random_string(30)
                    file = File.create(user_id=current_user.id, path=file_path)

                    s3.upload_from_bytesio(
                        file_path, BytesIO(form.profile_picture.data.read())
                    )

                    Session.flush()
                    LOG.d("upload file %s to s3", file)

                    current_user.profile_picture_id = file.id
                    Session.commit()
                    profile_updated = True

                if profile_updated:
                    flash("Your profile has been updated", "success")
                    return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "change-password":
            flash(
                "You are going to receive an email containing instructions to change your password",
                "success",
            )
            send_reset_password_email(current_user)
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "notification-preference":
            choose = request.form.get("notification")
            if choose == "on":
                current_user.notification = True
            else:
                current_user.notification = False
            Session.commit()
            flash("Your notification preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "change-alias-generator":
            scheme = int(request.form.get("alias-generator-scheme"))
            if AliasGeneratorEnum.has_value(scheme):
                current_user.alias_generator = scheme
                Session.commit()
            flash("Your preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "change-random-alias-default-domain":
            default_domain = request.form.get("random-alias-default-domain")

            if default_domain:
                sl_domain: SLDomain = SLDomain.get_by(domain=default_domain)
                if sl_domain:
                    if sl_domain.premium_only and not current_user.is_premium():
                        flash("You cannot use this domain", "error")
                        return redirect(url_for("dashboard.setting"))

                    current_user.default_alias_public_domain_id = sl_domain.id
                    current_user.default_alias_custom_domain_id = None
                else:
                    custom_domain = CustomDomain.get_by(domain=default_domain)
                    if custom_domain:
                        # sanity check
                        if (
                            custom_domain.user_id != current_user.id
                            or not custom_domain.verified
                        ):
                            LOG.w(
                                "%s cannot use domain %s", current_user, custom_domain
                            )
                            flash(f"Domain {default_domain} can't be used", "error")
                            return redirect(request.url)
                        else:
                            current_user.default_alias_custom_domain_id = (
                                custom_domain.id
                            )
                            current_user.default_alias_public_domain_id = None

            else:
                current_user.default_alias_custom_domain_id = None
                current_user.default_alias_public_domain_id = None

            Session.commit()
            flash("Your preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "random-alias-suffix":
            scheme = int(request.form.get("random-alias-suffix-generator"))
            if AliasSuffixEnum.has_value(scheme):
                current_user.random_alias_suffix = scheme
                Session.commit()
            flash("Your preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "change-sender-format":
            sender_format = int(request.form.get("sender-format"))
            if SenderFormatEnum.has_value(sender_format):
                current_user.sender_format = sender_format
                current_user.sender_format_updated_at = arrow.now()
                Session.commit()
                flash("Your sender format preference has been updated", "success")
            Session.commit()
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "replace-ra":
            choose = request.form.get("replace-ra")
            if choose == "on":
                current_user.replace_reverse_alias = True
            else:
                current_user.replace_reverse_alias = False
            Session.commit()
            flash("Your preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "sender-in-ra":
            choose = request.form.get("enable")
            if choose == "on":
                current_user.include_sender_in_reverse_alias = True
            else:
                current_user.include_sender_in_reverse_alias = False
            Session.commit()
            flash("Your preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "expand-alias-info":
            choose = request.form.get("enable")
            if choose == "on":
                current_user.expand_alias_info = True
            else:
                current_user.expand_alias_info = False
            Session.commit()
            flash("Your preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))
        elif request.form.get("form-name") == "ignore-loop-email":
            choose = request.form.get("enable")
            if choose == "on":
                current_user.ignore_loop_email = True
            else:
                current_user.ignore_loop_email = False
            Session.commit()
            flash("Your preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))
        elif request.form.get("form-name") == "one-click-unsubscribe":
            choose = request.form.get("enable")
            if choose == "on":
                current_user.one_click_unsubscribe_block_sender = True
            else:
                current_user.one_click_unsubscribe_block_sender = False
            Session.commit()
            flash("Your preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))
        elif request.form.get("form-name") == "include_website_in_one_click_alias":
            choose = request.form.get("enable")
            if choose == "on":
                current_user.include_website_in_one_click_alias = True
            else:
                current_user.include_website_in_one_click_alias = False
            Session.commit()
            flash("Your preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))
        elif request.form.get("form-name") == "change-blocked-behaviour":
            choose = request.form.get("blocked-behaviour")
            if choose == str(BlockBehaviourEnum.return_2xx.value):
                current_user.block_behaviour = BlockBehaviourEnum.return_2xx.name
            elif choose == str(BlockBehaviourEnum.return_5xx.value):
                current_user.block_behaviour = BlockBehaviourEnum.return_5xx.name
            else:
                flash("There was an error. Please try again", "warning")
                return redirect(url_for("dashboard.setting"))
            Session.commit()
            flash("Your preference has been updated", "success")
        elif request.form.get("form-name") == "sender-header":
            choose = request.form.get("enable")
            if choose == "on":
                current_user.include_header_email_header = True
            else:
                current_user.include_header_email_header = False
            Session.commit()
            flash("Your preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))
        elif request.form.get("form-name") == "export-data":
            return redirect(url_for("api.export_data"))
        elif request.form.get("form-name") == "export-alias":
            return redirect(url_for("api.export_aliases"))

    manual_sub = ManualSubscription.get_by(user_id=current_user.id)
    apple_sub = AppleSubscription.get_by(user_id=current_user.id)
    coinbase_sub = CoinbaseSubscription.get_by(user_id=current_user.id)

    return render_template(
        "dashboard/setting.html",
        form=form,
        PlanEnum=PlanEnum,
        SenderFormatEnum=SenderFormatEnum,
        BlockBehaviourEnum=BlockBehaviourEnum,
        promo_form=promo_form,
        change_email_form=change_email_form,
        pending_email=pending_email,
        AliasGeneratorEnum=AliasGeneratorEnum,
        manual_sub=manual_sub,
        apple_sub=apple_sub,
        coinbase_sub=coinbase_sub,
        FIRST_ALIAS_DOMAIN=FIRST_ALIAS_DOMAIN,
        ALIAS_RAND_SUFFIX_LENGTH=ALIAS_RANDOM_SUFFIX_LENGTH,
    )


def send_reset_password_email(user):
    """
    generate a new ResetPasswordCode and send it over email to user
    """
    # the activation code is valid for 1h
    reset_password_code = ResetPasswordCode.create(
        user_id=user.id, code=random_string(60)
    )
    Session.commit()

    reset_password_link = f"{URL}/auth/reset_password?code={reset_password_code.code}"

    email_utils.send_reset_password_email(user.email, reset_password_link)


def send_change_email_confirmation(user: User, email_change: EmailChange):
    """
    send confirmation email to the new email address
    """

    link = f"{URL}/auth/change_email?code={email_change.code}"

    email_utils.send_change_email(email_change.new_email, user.email, link)


@dashboard_bp.route("/resend_email_change", methods=["GET", "POST"])
@login_required
def resend_email_change():
    email_change = EmailChange.get_by(user_id=current_user.id)
    if email_change:
        # extend email change expiration
        email_change.expired = arrow.now().shift(hours=12)
        Session.commit()

        send_change_email_confirmation(current_user, email_change)
        flash("A confirmation email is on the way, please check your inbox", "success")
        return redirect(url_for("dashboard.setting"))
    else:
        flash(
            "You have no pending email change. Redirect back to Setting page", "warning"
        )
        return redirect(url_for("dashboard.setting"))


@dashboard_bp.route("/cancel_email_change", methods=["GET", "POST"])
@login_required
def cancel_email_change():
    email_change = EmailChange.get_by(user_id=current_user.id)
    if email_change:
        EmailChange.delete(email_change.id)
        Session.commit()
        flash("Your email change is cancelled", "success")
        return redirect(url_for("dashboard.setting"))
    else:
        flash(
            "You have no pending email change. Redirect back to Setting page", "warning"
        )
        return redirect(url_for("dashboard.setting"))
