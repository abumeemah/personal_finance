@credits_bp.route('/requests', methods=['GET'])
@login_required
@utils.limiter.limit("100 per hour")
@utils.limiter.limit("20 per hour")
def view_credit_requests():