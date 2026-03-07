def compute_core_score(df):

    df["core_score"] = (
        df["z_rev_cagr"] +
        df["z_operating_margin"] +
        df["z_roe"] +
        df["z_fcf_margin"] -
        df["z_debt_ratio"]
    )

    return df


def compute_total_score(df):
    df["total_score"] = df["core_score"] + df.get("pattern_bonus", 0)
    return df
