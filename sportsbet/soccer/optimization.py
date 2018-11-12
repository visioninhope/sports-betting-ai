"""
Includes classes and functions to test and select the optimal 
betting strategy on historical and current data.
"""

# Author: Georgios Douzas <gdouzas@icloud.com>
# License: BSD 3 clause

from pathlib import Path
from os.path import join
from pickle import dump, load

import numpy as np
import pandas as pd
from sklearn.utils import check_array, check_X_y
from sklearn.model_selection import GridSearchCV, StratifiedKFold
from sklearn.model_selection._split import BaseCrossValidator, _num_samples
from sklearn.metrics import precision_score
from sklearn.utils import Parallel, delayed
from tqdm import tqdm

from .. import PATH
from ..utils import ProfitEstimator, mean_profit_score, set_random_state, _fit_predict
from .data import (
    _fetch_historical_spi_data, 
    _fetch_historical_fd_data, 
    _fetch_predictions_spi_data,
    _fetch_predictions_fd_data,
    _match_teams_names,
    LEAGUES_MAPPING
)
from ..config import DEFAULT_CLASSIFIERS

TRAINING_DATA_PATH = join(PATH, 'training_data.csv')
PREDICTIONS_DATA_PATH = join(PATH, 'predictions_data.csv')


class SeasonTimeSeriesSplit(BaseCrossValidator):
    """Season time series cross-validator.
    Parameters
    ----------
    test_season : str, default='17-18'
        The testing season.
    max_day_range: int
        The maximum day range of each test fold.
    """

    def __init__(self, test_year=2, max_day_range=6):
        self.test_year = test_year
        self.max_day_range = max_day_range

    def _generate_season_indices(self, X):
        """Generate season indices to use in test set."""

        # Check input array
        X = check_array(X, dtype=None)
        
        # Define days
        self.days_ = X[:, 0]

        # Define all and season indices
        indices = np.arange(_num_samples(X))
        start_day, end_day = 365 * (self.test_year - 1), 365 * self.test_year
        season_indices = indices[(self.days_ >= start_day) & (self.days_ < end_day)]

        return season_indices


    def split(self, X, y=None, groups=None):
        """Generates indices to split data into training and test set.
        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Training data, where n_samples is the number of samples
            and n_features is the number of features.
        y : array-like, shape (n_samples,)
            Always ignored, exists for compatibility.
        groups : array-like, with shape (n_samples,), optional
            Always ignored, exists for compatibility.
        Returns
        -------
        train_indices : ndarray
            The training set indices for that split.
        test_indices : ndarray
            The testing set indices for that split.
        """

        # Generate season indices
        season_indices = self._generate_season_indices(X)

        # Yield train and test indices
        start_ind = season_indices[0]
        for ind in season_indices:
            if self.days_[ind] - self.days_[start_ind] >= self.max_day_range:
                train_indices = np.arange(0, start_ind)
                test_indices = np.arange(start_ind, ind)
                start_ind = ind
                yield (train_indices, test_indices)

    def get_n_splits(self, X, y=None, groups=None):
        """Returns the number of splitting iterations in the cross-validator

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Training data, where n_samples is the number of samples
            and n_features is the number of features.

        y : object
            Always ignored, exists for compatibility.

        groups : object
            Always ignored, exists for compatibility.

        Returns
        -------
        n_splits : int
            Returns the number of splitting iterations in the cross-validator.
        """

        # Generate season indices
        season_indices = self._generate_season_indices(X)

        # Calculate number of splits
        start_ind, n_splits = season_indices[0], 0
        for ind in season_indices:
            if self.days_[ind] - self.days_[start_ind] >= self.max_day_range:
                n_splits += 1
                start_ind = ind
        
        return n_splits


class BettingAgent:

    @staticmethod
    def _validate_leagues(leagues):
        """Validate leagues input."""
        valid_leagues = [league_id for league_id in LEAGUES_MAPPING.keys()]
        if leagues != 'all' and not set(leagues).issubset(valid_leagues):
            msg = "The `leagues` parameter should be either equal to 'all' or a list of valid league ids. Got {} instead."
            raise ValueError(msg.format(leagues))

    @staticmethod
    def _check_classifier(classifier, fit_params):

        # Check classifier and its fitting parameters
        classifier = ProfitEstimator(classifier) if classifier is not None else ProfitEstimator(DEFAULT_CLASSIFIERS['trivial'][0])
        fit_params = fit_params.copy() if fit_params is not None else DEFAULT_CLASSIFIERS['trivial'][1]

        return classifier, fit_params

    def _fetch_data(self, leagues, data_type):
        """Fetch the data."""

        # Validate leagues
        self._validate_leagues(leagues)

        # Define parameters 
        avg_odds_features = ['HomeAverageOdd', 'AwayAverageOdd', 'DrawAverageOdd']
        functions_mapping = {
            'historical': [_fetch_historical_spi_data, _fetch_historical_fd_data],
            'predictions': [_fetch_predictions_spi_data, _fetch_predictions_fd_data]
        }

        # Fetch data
        functions = functions_mapping[data_type]
        spi_data, fd_data = functions[0](leagues), functions[1](leagues)

        # Teams names matching
        mapping = _match_teams_names(spi_data, fd_data)
        spi_data['HomeTeam'] = spi_data['HomeTeam'].apply(lambda team: mapping[team] if team in mapping.keys() else team)
        spi_data['AwayTeam'] = spi_data['AwayTeam'].apply(lambda team: mapping[team] if team in mapping.keys() else team)

        # Probabilities data
        probs = 1 / fd_data.loc[:, avg_odds_features].values
        probs = pd.DataFrame(probs / probs.sum(axis=1)[:, None], columns=['HomeFDProb', 'AwayFDProb', 'DrawFDProb'])
        probs_data = pd.concat([probs, fd_data], axis=1)

        return spi_data, probs_data

    def fetch_training_data(self, leagues):
        """Fetch the training data."""

        # Fetch data
        spi_data, probs_data = self._fetch_data(leagues, 'historical')

        # Define merge keys
        keys = ['Date', 'League', 'HomeTeam', 'AwayTeam', 'HomeGoals', 'AwayGoals']

        # Combine data
        training_data = pd.merge(spi_data, probs_data, on=keys)

        # Create features
        training_data['DiffSPIGoals'] = training_data['HomeSPIGoals'] - training_data['AwaySPIGoals']
        training_data['DiffSPI'] = training_data['HomeSPI'] - training_data['AwaySPI']
        training_data['DiffSPIProb'] = training_data['HomeSPIProb'] - training_data['AwaySPIProb']
        training_data['DiffFDProb'] = training_data['HomeFDProb'] - training_data['AwayFDProb']

        # Create day index
        training_data['Day'] = (training_data.Date - min(training_data.Date)).dt.days

        # Sort data
        training_data = training_data.sort_values(keys[:-2]).reset_index(drop=True)

        # Drop features
        training_data.drop(columns=['Date', 'League', 'HomeTeam', 'AwayTeam'], inplace=True)

        # Save data
        Path(PATH).mkdir(exist_ok=True)
        training_data.to_csv(TRAINING_DATA_PATH, index=False)
    
    def fetch_predictions_data(self, leagues):
        """Fetch the predictions data."""

        # Fetch data
        spi_data, probs_data = self._fetch_data(leagues, 'predictions')

        # Define merge keys
        keys = ['League', 'HomeTeam', 'AwayTeam']

        # Combine data
        predictions_data = pd.merge(spi_data.drop(columns=['HomeGoals', 'AwayGoals']), probs_data.drop(columns=['Date', 'HomeGoals', 'AwayGoals']), on=keys)

        # Create features
        predictions_data['DiffSPIGoals'] = predictions_data['HomeSPIGoals'] - predictions_data['AwaySPIGoals']
        predictions_data['DiffSPI'] = predictions_data['HomeSPI'] - predictions_data['AwaySPI']
        predictions_data['DiffSPIProb'] = predictions_data['HomeSPIProb'] - predictions_data['AwaySPIProb']
        predictions_data['DiffFDProb'] = predictions_data['HomeFDProb'] - predictions_data['AwayFDProb']

        # Sort data
        predictions_data = predictions_data.sort_values(['Date'] + keys).reset_index(drop=True)

        # Save data
        Path(PATH).mkdir(exist_ok=True)
        predictions_data.to_csv(PREDICTIONS_DATA_PATH, index=False)

    def load_training_data(self, predicted_result, odds_type):
        """Load the data used for model training."""

        # Load data
        try:
            training_data = pd.read_csv(TRAINING_DATA_PATH)
        except FileNotFoundError:
            raise FileNotFoundError('Training data do not exist. Fetch training data before loading modeling data.')

        # Define odds columns
        odds_columns = {
            'average': ['HomeAverageOdd', 'AwayAverageOdd', 'DrawAverageOdd'], 
            'maximum': ['HomeMaximumOdd', 'AwayMaximumOdd', 'DrawMaximumOdd']
        }

        # Define predicted results
        predicted_results = list(predicted_result)

        # Input data
        X = training_data.drop(columns=odds_columns['average'] +  odds_columns['maximum'] + ['HomeGoals', 'AwayGoals'])
        X = X[['Day'] + X.columns[:-1].tolist()]
        
        # Target
        y = (training_data['HomeGoals'] - training_data['AwayGoals']).apply(lambda sign: 'H' if sign > 0 else 'D' if sign == 0 else 'A')
        y = y.apply(lambda result: '-' if result not in predicted_results else result)
        
        # Check arrays
        X, y = check_X_y(X, y)

        # Odds
        if odds_type is not None:
            odds = training_data.loc[:, odds_columns[odds_type]]
            odds = check_array(odds)
            return X, y, odds
        else:
            return X, y

    def load_predictions_data(self):
        """Load the data used for model predictions."""

        # Load data
        try:
            predictions_data = pd.read_csv(PREDICTIONS_DATA_PATH)
        except FileNotFoundError:
            raise FileNotFoundError('Predictions data do not exist. Fetch predictions data before loading modeling data.')

        # Define odds columns
        odds_columns = ['Date', 'League', 'HomeTeam', 'AwayTeam', 'HomeAverageOdd', 'AwayAverageOdd', 'DrawAverageOdd', 'HomeMaximumOdd', 'AwayMaximumOdd', 'DrawMaximumOdd']

        # Input data
        X = predictions_data.drop(columns=odds_columns)
        
        # Define odds dataframe
        odds = predictions_data.loc[:, odds_columns]

        # Check arrays
        X = check_array(X)

        return X, odds

    def _load_prepare_data(self, predicted_result, odds_type):

        # Load modelling data
        X, y, odds = self.load_training_data(predicted_result, odds_type)

        # Prepare data
        X = np.hstack((X, odds))

        return X, y

    def cross_validate(self, classifier, fit_params, predicted_result, n_splits, odds_type, random_state):
        """Evaluate classifier performance using cross-validation."""

        # Load and prepare data
        X, y = self._load_prepare_data(predicted_result, odds_type)

        # Check classifier and its fitting parameters
        classifier, fit_params = self._check_classifier(classifier, fit_params)

        # Set random state
        set_random_state(classifier, random_state)

        # Run cross-validation
        gscv = GridSearchCV(estimator=classifier, param_grid={}, scoring='mean_profit', 
                            cv=StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=random_state), 
                            n_jobs=-1, refit=False, iid=True, return_train_score=False)
        gscv.fit(X, y, **fit_params)

        # Extract results
        columns = ['mean_test_score', 'std_test_score']
        results = pd.DataFrame(gscv.cv_results_)[columns].values.reshape(-1).tolist()

        return results

    def backtest(self, classifier, fit_params, predicted_result, test_year, max_day_range, odds_type):
        """Apply backtesting to betting agent."""

        # Load and prepare data
        X, y = self._load_prepare_data(predicted_result, odds_type)

        # Check classifier and its fitting parameters
        classifier, fit_params = self._check_classifier(classifier, fit_params)

        # Define train and test indices
        indices = list(SeasonTimeSeriesSplit(test_year=test_year, max_day_range=max_day_range).split(X, y))

        # Run cross-validation
        self.backtest_results_ = Parallel(n_jobs=-1)(delayed(_fit_predict)(classifier, 
                                                                           X, y, 
                                                                           train_indices, test_indices, 
                                                                           **fit_params)
                                          for train_indices, test_indices in tqdm(indices, desc='Backtesting: '))

    def calculate_backtest_results(self, bet_factor=2, credit_exponent=3):
        """Calculate the results of backtesting."""

        # Initialize parameters
        statistics, precisions = [], []
        capital, bet_amount = 1.0, 1.0
        y_test_all, y_pred_all, y_pred_proba_all, odds_all = np.array([]), np.array([]), np.array([]), np.array([])
        

        for y_test, ((y_pred, y_pred_proba), odds) in self.backtest_results_:

            # Append results, predictions and odds
            y_test_all, y_pred_all = np.hstack((y_test, y_test_all)), np.hstack((y_pred, y_pred_all))
            y_pred_proba_all = np.vstack((y_pred_proba, y_pred_proba_all.reshape(-1, y_pred_proba.shape[1])))
            odds_all = np.vstack((odds, odds_all.reshape(-1, odds.shape[1])))

            # Calculate number of bets and matches
            mask = (y_pred != '-')
            n_bets = mask.sum()
            n_matches = y_pred.size

            # Calculate precision
            precision = precision_score(y_test[mask], y_pred[mask], average='micro') if n_bets > 0 else np.nan
            precisions.append(precision)

            # Calculate profit
            profit = bet_amount * mean_profit_score(y_test, ((y_pred, y_pred_proba), odds))
            
            # Calculate capital
            capital += profit

            # Adjust bet amount
            bet_amount = bet_amount * bet_factor if profit < 0.0 else 1.0

            # Calculate credit
            max_credit = capital + bet_factor ** credit_exponent
            if bet_amount > max_credit:
                bet_amount = max_credit
                
            # Generate statistic
            statistic = (capital, profit, bet_amount, n_bets, n_matches, precision)

            # Append statistic
            statistics.append(statistic)

            if bet_amount == 0:
                break

        # Define statistics dataframe
        statistics = pd.DataFrame(statistics, columns=['Capital', 'Profit', 'Bet amount', 'Bets', 'Matches', 'Precision'])

        # Define attributes
        mean_precision = np.nanmean(precisions)
        profit_per_bet = mean_profit_score(y_test_all, ((y_pred_all, y_pred_proba_all), odds_all))

        return statistics, mean_precision, profit_per_bet

    def fit_dump_classifier(self, predicted_result, classifier, fit_params, clf_name):
        """Fit and dump a classifier."""

        # Load modelling data
        X, y = self.load_training_data(predicted_result, None)

        # Remove time index
        X = X[:, 1:]

        # Fit classifier
        classifier.fit(X, y, **fit_params)

        # Dump classifier
        with open(join(PATH, '%s.pkl' % clf_name) , 'wb') as file:
            dump(classifier, file)
    
    def predict(self, clf_name):
        """Generate predictions using a fitted classifier."""

        # Load predictions data
        X, odds = self.load_predictions_data()

        # Load classifier
        with open(join(PATH, '%s.pkl' % clf_name) , 'rb') as file:
            classifier = load(file)

        # Generate predictions
        y_pred = pd.DataFrame(classifier.predict(X), columns=['Prediction'])

        # Stack predictions
        predictions = pd.concat([odds, y_pred], axis=1)

        return predictions